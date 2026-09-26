# Generate the USP protobuf bindings from the Broadband Forum schemas the
# controller uses, so the device can decode records it observes on the broker
FROM python:3.12-slim AS proto
RUN pip install --no-cache-dir grpcio-tools==1.69.0
COPY controller/usp_proto/ /proto/
RUN mkdir -p /out && python -m grpc_tools.protoc -I /proto --python_out=/out /proto/usp_msg.proto /proto/usp_record.proto

FROM python:3.12-slim

WORKDIR /app

COPY device/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY device/vdev/ ./vdev/
COPY device/static/ ./static/
COPY --from=proto /out/*_pb2.py ./vdev/usp_proto/
# The controller library the tests use, for the lab's own controller
COPY controller/uspctl/__init__.py controller/uspctl/client.py ./uspctl/
COPY controller/uspctl/proto/__init__.py ./uspctl/proto/
COPY --from=proto /out/*_pb2.py ./uspctl/proto/

ENV VDEV_SOCK=/run/vdev/vdev.sock
ENV VDEV_HTTP_PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080 1883

CMD ["python", "-m", "vdev.main"]
