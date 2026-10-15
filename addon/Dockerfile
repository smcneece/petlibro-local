# build 2026.08.1
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.10.0

RUN apk add --no-cache ffmpeg

RUN pip3 install --no-cache-dir aiohttp==3.14.5 aiomqtt==2.5.1 tzdata==2026.5

COPY app/ /app/
COPY icon.png /app/icon.png

COPY run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
