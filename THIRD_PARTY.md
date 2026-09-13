# Third-party software

obuspa-lab is not affiliated with or endorsed by the Broadband Forum. It
builds on their work as follows.

## OB-USP-AGENT (obuspa)

<https://github.com/BroadbandForum/obuspa> — BSD-3-Clause.

Built from source **at image build time** (`agent/Dockerfile`, pinned to a
release tag) and run unmodified. Not redistributed in this repository. Vendor
plug-ins are compiled against its headers.

## USP protocol buffer schemas

<https://github.com/BroadbandForum/usp> — BSD-3-Clause.

`controller/usp_proto/usp_msg.proto` and `usp_record.proto` are the Broadband
Forum's `usp-msg-1-5.proto` and `usp-record-1-5.proto`, redistributed with
their copyright headers intact. They are renamed only because Python cannot
import module names containing dashes; no content is changed.

## three.js

<https://github.com/mrdoob/three.js> — MIT.

`device/static/vendor/three.module.js` and `OrbitControls.js` (r160) are
vendored unmodified, with their license headers, so the UI works offline.

## Eclipse Mosquitto

<https://mosquitto.org> — EPL-2.0 / EDL-1.0. Pulled as a container image
(`eclipse-mosquitto:2`); not redistributed.

## Python dependencies

FastAPI, uvicorn, paho-mqtt, protobuf, grpcio-tools, pytest, requests — see
`device/requirements.txt` and `controller/requirements.txt`. Installed at
build time; not redistributed.
