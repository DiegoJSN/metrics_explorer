FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias del sistema (PostgreSQL libs y utilidades básicas)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl gettext \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python
COPY requirements-demo.txt /app/requirements-demo.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements-demo.txt

# Copiar el código
COPY . /app

EXPOSE 8000
CMD ["sh", "/app/docker-entrypoint.sh"]
