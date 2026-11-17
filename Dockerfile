FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg mkvtoolnix tzdata
COPY audiofix.py /app/audiofix.py
ENV PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8787
ENTRYPOINT ["python3", "/app/audiofix.py"]
