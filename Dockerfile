# The debate-ref server. Profile comes from the command (see deploy/compose.yml); keys from the environment.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_ROOT_USER_ACTION=ignore PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements/ requirements/
# Every role's requirements except dev (pytest).
RUN pip install -r requirements/base.txt -r requirements/brain.txt -r requirements/ears.txt \
                -r requirements/evidence.txt -r requirements/face.txt
COPY app/ app/
COPY fixtures/ fixtures/

RUN useradd --system --no-create-home app && mkdir logs cache && chown app:app logs cache
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["python", "-m", "app.main", "mock"]
