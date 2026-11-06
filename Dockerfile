FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Europe/Vienna
WORKDIR /app
COPY *.py ./
COPY web/ web/

USER nobody
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "mh_status_web.py", "--host", "0.0.0.0", "--port", "8080"]
