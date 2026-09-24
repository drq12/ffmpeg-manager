FROM python:3.12-slim-bookworm
# nsenter (util-linux) is already in the base image; ffmpeg and nvidia-smi are taken from the host
WORKDIR /app
COPY app/ /app/
ENV DATA_DIR=/data PORT=8080
VOLUME /data
CMD ["python3", "-u", "server.py"]
