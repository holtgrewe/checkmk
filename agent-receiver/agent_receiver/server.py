#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import os
import shutil
from pathlib import Path
from tempfile import mkstemp
from typing import Dict, Mapping, Optional

from agent_receiver.checkmk_rest_api import host_exists, post_csr
from agent_receiver.constants import AGENT_OUTPUT_DIR, DATA_SOURCE_DIR, REGISTRATION_REQUESTS
from agent_receiver.log import logger
from cryptography.x509 import load_pem_x509_csr
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel
from starlette.status import HTTP_204_NO_CONTENT, HTTP_404_NOT_FOUND

app = FastAPI()


class PairingBody(BaseModel):
    csr: str


def _uuid_from_pem_csr(pem_csr: str) -> str:
    try:
        return (
            load_pem_x509_csr(pem_csr.encode())
            .subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0]
            .value
        )
    except ValueError:
        return "[CSR parsing failed]"


@app.post("/pairing")
async def pairing(
    *,
    authentication: Optional[str] = Header(None),
    pairing_body: PairingBody,
) -> Mapping[str, str]:
    rest_api_csr_resp = post_csr(
        str(authentication),
        pairing_body.csr,
    )
    if rest_api_csr_resp.ok:
        logger.info(
            "uuid=%s CSR signed",
            _uuid_from_pem_csr(pairing_body.csr),
        )
        return rest_api_csr_resp.json()

    logger.info(
        "uuid=%s CSR failed with %s",
        _uuid_from_pem_csr(pairing_body.csr),
        rest_api_csr_resp.text,
    )
    raise HTTPException(
        status_code=rest_api_csr_resp.status_code,
        detail=rest_api_csr_resp.text,
    )


class RegistrationWithHNBody(BaseModel):
    uuid: str
    host_name: str


def _create_link(
    *,
    source_dir: Path,
    target_dir: Path,
    uuid: str,
    host_name: str,
) -> None:
    (source_dir / uuid).symlink_to(target_dir / host_name)


@app.post(
    "/register_with_hostname",
    status_code=HTTP_204_NO_CONTENT,
)
async def register_with_hostname(
    *,
    authentication: Optional[str] = Header(None),
    registration_body: RegistrationWithHNBody,
) -> Response:
    if not host_exists(
        str(authentication),
        registration_body.host_name,
    ):
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"Host {registration_body.host_name} does not exist",
        )
    _create_link(
        source_dir=AGENT_OUTPUT_DIR,
        target_dir=DATA_SOURCE_DIR,
        uuid=registration_body.uuid,
        host_name=registration_body.host_name,
    )
    logger.info(
        "uuid=%s registered host %s",
        registration_body.uuid,
        registration_body.host_name,
    )
    return Response(status_code=HTTP_204_NO_CONTENT)


def get_hostname(uuid: str) -> Optional[str]:
    link_path = AGENT_OUTPUT_DIR / uuid

    try:
        target_path = os.readlink(link_path)
    except FileNotFoundError:
        return None

    return Path(target_path).name


@app.post("/agent-data")
async def agent_data(uuid: str = Form(...), upload_file: UploadFile = File(...)) -> Dict[str, str]:
    file_dir = AGENT_OUTPUT_DIR / uuid
    file_path = file_dir / "received-output"

    try:
        file_handle, temp_path = mkstemp(dir=file_dir)
        with open(file_handle, "wb") as temp_file:
            shutil.copyfileobj(upload_file.file, temp_file)

        os.rename(temp_path, file_path)

    except FileNotFoundError:
        logger.error(
            "uuid=%s Host is not registered",
            uuid,
        )
        raise HTTPException(status_code=403, detail="Host is not registered")

    ready_file = REGISTRATION_REQUESTS / "READY" / f"{uuid}.json"
    hostname = get_hostname(uuid)

    if ready_file.exists() and hostname:
        try:
            shutil.move(ready_file, REGISTRATION_REQUESTS / "DISCOVERABLE" / f"{hostname}.json")
        except FileNotFoundError:
            logger.warning(
                "uuid=%s Could not move registration request from READY to DISCOVERABLE",
                uuid,
            )

    logger.info(
        "uuid=%s Agent data saved",
        uuid,
    )
    return {"message": "Agent data saved."}
