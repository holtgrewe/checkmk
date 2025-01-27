// Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
// This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
// conditions defined in the file COPYING, which is part of this source code package.

mod agent_receiver_api;
mod certs;
mod cli;
mod config;
mod monitoring_data;
mod tls_server;
use anyhow::{anyhow, Context, Result as AnyhowResult};
use config::RegistrationState;
use nix::unistd;
use std::fs;
use std::io::Result as IoResult;
use std::io::{self, Write};
use std::path::Path;
use structopt::StructOpt;
use uuid::Uuid;

use log::{info, LevelFilter};
use log4rs::append::file::FileAppender;
use log4rs::config::{Appender, Config, Root};
use log4rs::encode::pattern::PatternEncoder;

const CMK_AGENT_USER: &str = "cmk-agent";
const HOME_DIR: &str = "/var/lib/cmk-agent";
// Normally, the config would be expected at /etc/check_mk/, but we
// need to read it as cmk-agent user, so we use its home directory.
const CONFIG_FILE: &str = "cmk-agent-ctl-config.json";

const STATE_FILE: &str = "cmk-agent-ctl-state.json";
const LOG_FILE: &str = "cmk-agent-ctl.log";
const LEGACY_PULL_FILE: &str = "allow-legacy-pull";
const TLS_ID: &[u8] = b"16";

fn register(
    config: config::Config,
    mut reg_state: RegistrationState,
    path_state_out: &Path,
) -> AnyhowResult<()> {
    let agent_receiver_address = config
        .agent_receiver_address
        .context("Server addresses not specified.")?;
    let credentials = config
        .credentials
        .context("Missing credentials for registration.")?;
    let host_name = config
        .host_name
        .context("Missing host name for registration")?;

    let uuid = Uuid::new_v4().to_string();
    // TODO: what if registration_state.contains_key(agent_receiver_address) (already registered)?
    let root_cert = match &config.root_certificate {
        Some(cert) => cert.clone(),
        None => certs::fetch_root_cert(&agent_receiver_address)
            .context(format!("Error establishing trust with agent_receiver."))?,
    };

    let (csr, private_key) = certs::make_csr(&uuid).context(format!("Error creating CSR."))?;
    let certificate =
        agent_receiver_api::pairing(&agent_receiver_address, &root_cert, csr, &credentials)
            .context(format!("Error pairing with {}", &agent_receiver_address))?;

    agent_receiver_api::register_with_hostname(
        &agent_receiver_address,
        &root_cert,
        &credentials,
        &uuid,
        &host_name,
    )
    .context(format!("Error registering {}", &agent_receiver_address))?;

    reg_state.server_specs.insert(
        agent_receiver_address,
        config::ServerSpec {
            uuid,
            private_key,
            certificate,
            root_cert,
        },
    );

    reg_state.to_file(path_state_out).unwrap();

    disallow_legacy_pull()
        .context("Registration successful, but could not delete marker for legacy pull mode")?;
    Ok(())
}

fn push(config: config::Config, reg_state: config::RegistrationState) -> AnyhowResult<()> {
    let mon_data = monitoring_data::collect(config.package_name)
        .context("Error collecting monitoring data")?;

    for (agent_receiver_address, server_spec) in reg_state.server_specs.iter() {
        let message =
            agent_receiver_api::agent_data(agent_receiver_address, &server_spec.uuid, &mon_data)
                .context(format!("Error pushing monitoring data."))?;
        println!("{}", message);
    }

    Ok(())
}

fn dump(config: config::Config) -> AnyhowResult<()> {
    let mon_data = monitoring_data::collect(config.package_name)
        .context("Error collecting monitoring data.")?;
    io::stdout()
        .write_all(&mon_data)
        .context("Error writing monitoring data to stdout.")?;

    Ok(())
}

fn status(_config: config::Config) -> AnyhowResult<()> {
    Err(anyhow!("Status mode not yet implemented"))
}

fn pull(config: config::Config, reg_state: config::RegistrationState) -> AnyhowResult<()> {
    if is_legacy_pull(&reg_state) {
        return dump(config);
    }

    let mut stream = tls_server::IoStream::new();

    stream.write(TLS_ID).unwrap();
    stream.flush().unwrap();

    let mut tls_connection =
        tls_server::tls_connection(reg_state).context("Could not initialize TLS.")?;
    let mut tls_stream = tls_server::tls_stream(&mut tls_connection, &mut stream);

    let mon_data = monitoring_data::collect(config.package_name)
        .context("Error collecting monitoring data.")?;
    tls_stream.write_all(&mon_data).unwrap();
    tls_stream.flush().unwrap();

    disallow_legacy_pull().context("Just provided agent data via TLS, but legacy pull mode is still allowed, and could not delete marker")?;
    Ok(())
}

fn is_legacy_pull(reg_state: &config::RegistrationState) -> bool {
    if !Path::new(HOME_DIR).join(LEGACY_PULL_FILE).exists() {
        return false;
    }
    if !reg_state.server_specs.is_empty() {
        return false;
    }
    true
}

fn disallow_legacy_pull() -> IoResult<()> {
    let legacy_pull_marker = Path::new(HOME_DIR).join(LEGACY_PULL_FILE);
    if !legacy_pull_marker.exists() {
        return Ok(());
    }

    fs::remove_file(legacy_pull_marker)
}

fn get_configuration(path_config: &Path, args: cli::Args) -> io::Result<config::Config> {
    return Ok(config::Config::merge_two_configs(
        config::Config::from_file(path_config)?,
        config::Config::from_args(args),
    ));
}

fn get_reg_state(path: &Path) -> io::Result<config::RegistrationState> {
    return Ok(config::RegistrationState::from_file(path)?);
}

fn init_logging(path: &Path) -> AnyhowResult<()> {
    let logfile = FileAppender::builder()
        .encoder(Box::new(PatternEncoder::new("{l} - {m}\n")))
        .build(path)?;

    let config = Config::builder()
        .appender(Appender::builder().build("logfile", Box::new(logfile)))
        .build(Root::builder().appender("logfile").build(LevelFilter::Info))?;

    log4rs::init_config(config)?;

    Ok(())
}

fn ensure_home_directory(path: &Path) -> io::Result<()> {
    if !path.exists() {
        fs::create_dir_all(path)?;
    }
    Ok(())
}

fn sanitize_home_dir_ownership(paths: [&Path; 4], user: &str) -> AnyhowResult<()> {
    if !unistd::Uid::current().is_root() {
        return Ok(());
    }

    let cmk_agent_user =
        unistd::User::from_name(user)?.context(format!("Could not find user {}", user))?;
    let cmk_agent_group =
        unistd::Group::from_name(user)?.context(format!("Could not find group {}", user))?;

    for path in paths {
        if path.exists() {
            unistd::chown(path, Some(cmk_agent_user.uid), Some(cmk_agent_group.gid))?;
        }
    }

    Ok(())
}

fn main() -> AnyhowResult<()> {
    let state_path = Path::new(HOME_DIR).join(STATE_FILE);
    let config_path = Path::new(HOME_DIR).join(CONFIG_FILE);
    let log_path = Path::new(HOME_DIR).join(LOG_FILE);

    // TODO: Decide: Check if running as cmk-agent or root, and abort otherwise?
    ensure_home_directory(Path::new(HOME_DIR))
        .context("Cannot go on: Missing cmk-agent home directory and failed to create it.")?;

    if let Err(error) = init_logging(&log_path).context("Failed to initialize logging") {
        println!("Error: {:?}", error)
    };
    info!("Starting cmk-agent-ctl");

    let args = cli::Args::from_args();
    let mode = String::from(&args.mode);

    let config =
        get_configuration(&config_path, args).context("Error while obtaining configuration.")?;
    let reg_state =
        get_reg_state(&state_path).context("Error while obtaining registration state.")?;

    let result = match mode.as_str() {
        "dump" => dump(config),
        "register" => register(config, reg_state, &state_path),
        "push" => push(config, reg_state),
        "status" => status(config),
        "pull" => pull(config, reg_state),
        _ => Err(anyhow!("Invalid mode: {}", mode)),
    };

    if let Err(error) = sanitize_home_dir_ownership(
        [Path::new(HOME_DIR), &state_path, &config_path, &log_path],
        CMK_AGENT_USER,
    )
    .context(format!(
        "Failed to set ownership of {} to {}",
        HOME_DIR, CMK_AGENT_USER
    )) {
        info!("{:?}", error)
    };

    // TODO: At least in pull and dump mode, we can't just pass an error here,
    // because the fetcher will receive the error as agent output.
    result
}
