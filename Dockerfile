FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Berlin
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY evccplan ./evccplan
RUN useradd -r -u 1000 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 80
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:80/healthz',timeout=3)" || exit 1
CMD ["python", "-m", "evccplan", "serve", "--config", "/data/config.yaml"]
