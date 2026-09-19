
Tú dijiste:
# Guía completa para la Persona A: preparar la infraestructura y despliegue de Authorsite

Este documento describe, en español y en orden lógico, todos los pasos que debe seguir la Persona A para convertir un proyecto Django local en un servicio robusto en producción. Se tienen en cuenta prácticas modernas (2025), con PostgreSQL, Celery, Redis, Docker, Nginx con HTTPS, Sentry, *health checks* y limitación de tasa.

## 1. Preparación inicial

1. **Pre-requisitos**
   - Debes tener acceso a una máquina (local o servidor) con sistema operativo Linux.
   - Disponer de un nombre de dominio si vas a publicar el servicio en internet.
   - Tener instalado **Docker** y **Docker Compose** (versión 1.29+).
   - Contar con un archivo .env para variables de entorno (usuarios, contraseñas, claves, DSN de Sentry).
   - Mantener el repositorio actualizado en la rama app_infra.

2. **Clave secreta y configuración segura**
   - Genera una cadena aleatoria para SECRET_KEY y nunca la publiques.
   - Asegúrate de establecer DEBUG=False, configurar ALLOWED_HOSTS con tu dominio y mover las claves/contraseñas a variables de entorno.

## 2. Configurar bases de datos: PostgreSQL

1. **Instalar PostgreSQL**
   - En servidores Ubuntu/Debian: instala el paquete postgresql y postgresql-contrib.
   - Verifica la instalación ejecutando postgres -V y conecta con psql para gestionar bases y usuarios.
   - Alternativa: crea un servicio db en Docker Compose con la imagen postgres:16. Define variables de entorno POSTGRES_USER, POSTGRES_PASSWORD y POSTGRES_DB. Los datos se persistirán con un volumen (por ejemplo, postgres_data).

2. **Crear las bases**
   - Crea dos bases de datos: authorsite para la aplicación y authors_data para los datos del autor.
   - Desde psql, ejecuta:
     
sql
     CREATE DATABASE authorsite;
     CREATE DATABASE authors_data;
     CREATE USER authorsite WITH ENCRYPTED PASSWORD 'tu_password';
     GRANT ALL PRIVILEGES ON DATABASE authorsite TO authorsite;
     GRANT ALL PRIVILEGES ON DATABASE authors_data TO authorsite;


3. **Conector y dependencias**
   - Añade psycopg[binary] a tus dependencias (pip install "psycopg[binary]"==3.*).
   - En settings.py, importa os y define el diccionario DATABASES para apuntar a las bases de Postgres mediante variables de entorno. Por ejemplo:
     
python
     import os
     DATABASES = {
         "default": {
             "ENGINE": "django.db.backends.postgresql",
             "NAME": os.getenv("PG_DB", "authorsite"),
             "USER": os.getenv("PG_USER", "authorsite"),
             "PASSWORD": os.getenv("PG_PASSWORD", ""),
             "HOST": os.getenv("PG_HOST", "db"),
             "PORT": os.getenv("PG_PORT", "5432"),
         },
         "authors": {
             "ENGINE": "django.db.backends.postgresql",
             "NAME": os.getenv("PG_AUTHORS_DB", "authors_data"),
             "USER": os.getenv("PG_AUTHORS_USER", "authorsite"),
             "PASSWORD": os.getenv("PG_AUTHORS_PASSWORD", ""),
             "HOST": os.getenv("PG_AUTHORS_HOST", "db"),
             "PORT": os.getenv("PG_AUTHORS_PORT", "5432"),
         },
     }


4. **Migraciones**
   - Ejecuta python manage.py makemigrations y python manage.py migrate para aplicar las migraciones a Postgres.

## 3. Instalar y configurar Redis

1. **Iniciar Redis**
   - Opción rápida: ejecuta un contenedor de Redis con Docker:
     
bash
     docker run -p 6379:6379 --name some-redis -d redis

     Para verificar que está funcionando:
     
bash
     docker exec -it some-redis redis-cli ping

     Debe devolver PONG.
   - En un entorno de producción, define un servicio redis en tu docker-compose.yml con la imagen redis:7 y sin persistencia de disco, pues se usa como intermediario de cola.

2. **Uso como broker y backend**
   - Redis actuará como broker y backend para Celery. Configuraremos CELERY_BROKER_URL y CELERY_RESULT_BACKEND apuntando a redis://redis:6379/0 en el archivo settings.py.

## 4. Configurar Celery para tareas en segundo plano

1. **Instalar dependencias**
   - Añade celery y redis a las dependencias (pip install celery==5.* redis==5.*).

2. **Crear archivo authorsite/celery.py**
   - Este archivo inicializa la aplicación Celery y la configura con los ajustes de Django:
     
python
     import os
     from celery import Celery

     os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authorsite.settings")
     app = Celery("authorsite")
     app.config_from_object("django.conf:settings", namespace="CELERY")
     app.autodiscover_tasks()


3. **Inicializar Celery en authorsite/__init__.py**
   - Agrega:
     
python
     from .celery import app as celery_app
     __all__ = ("celery_app",)


4. **Configurar Celery en settings.py**
   - Añade variables de entorno para Celery que apunten a Redis:
     
python
     CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
     CELERY_RESULT_BACKEND = os.getenv("REDIS_URL", "redis://redis:6379/0")
     CELERY_TASK_TIME_LIMIT = 60*60  # 1 hora
     CELERY_TASK_SOFT_TIME_LIMIT = 60*50


5. **Crear tareas y workers**
   - Define tus tareas en profiles/tasks.py usando el decorador @shared_task y realiza la lógica del ETL.
   - Arranca un worker en un servicio separado (por ejemplo, servicio worker en Docker Compose) utilizando celery -A authorsite worker -l info.

## 5. Dockerizar la aplicación

1. **Escribir Dockerfile para la aplicación web**
   - Utiliza una imagen base de Python (p. ej. python:3.12-slim).
   - Copia tu código, instala dependencias con pip install -r requirements.txt y expone el puerto 8000.
   - Recuerda ejecutar python manage.py collectstatic durante la construcción para recopilar archivos estáticos.

2. **Crear docker-compose.yml**
   - Define los servicios:

     | Servicio | Función | Ejemplo de configuración |
     |---|---|---|
     | db | Contenedor PostgreSQL | Usa image: postgres:16, define variables POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB y un volumen postgres_data. |
     | redis | Servidor Redis para Celery | Usa image: redis:7 y expón el puerto 6379. |
     | web | Servidor Django con Gunicorn | Construye con Dockerfile y usa el comando gunicorn authorsite.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120. Pasa variables de entorno para bases de datos, Redis, SECRET_KEY y dominios permitidos. |
     | worker | Proceso Celery para tareas en segundo plano | Construye igual que web pero arranca con celery -A authorsite worker -l info. |

   - Ejemplo simplificado de YAML:
     
yaml
     version: "3.9"
     services:
       db:
         image: postgres:16
         environment:
           POSTGRES_DB: authorsite
           POSTGRES_USER: authorsite
           POSTGRES_PASSWORD: secret
         volumes:
           - postgres_data:/var/lib/postgresql/data

       redis:
         image: redis:7
         ports:
           - "6379:6379"

       web:
         build: .
         command: gunicorn authorsite.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120
         environment:
           DEBUG: "False"
           SECRET_KEY: ${SECRET_KEY}
           PG_DB: authorsite
           PG_USER: authorsite
           PG_PASSWORD: secret
           PG_HOST: db
           REDIS_URL: redis://redis:6379/0
           ALLOWED_HOSTS: "tudominio.com,www.tudominio.com"
           CSRF_TRUSTED_ORIGINS: "https://tudominio.com,https://www.tudominio.com"
         depends_on:
           - db
           - redis
         ports:
           - "8000:8000"

       worker:
         build: .
         command: celery -A authorsite worker -l info
         environment:
           DEBUG: "False"
           SECRET_KEY: ${SECRET_KEY}
           PG_DB: authorsite
           PG_USER: authorsite
           PG_PASSWORD: secret
           PG_HOST: db
           REDIS_URL: redis://redis:6379/0
         depends_on:
           - db
           - redis

     volumes:
       postgres_data:

   - Ejecuta docker-compose up --build para levantar todos los servicios.

## 6. Servir estáticos y preparar Nginx como reverse proxy

1. **Whitenoise (opción simple)**
   - Instala whitenoise (pip install whitenoise==6.*) y añade "whitenoise.middleware.WhiteNoiseMiddleware" al principio de la lista de MIDDLEWARE en settings.py.
   - Define STATIC_URL = "static/" y STATIC_ROOT = BASE_DIR / "staticfiles".
   - Ejecuta python manage.py collectstatic para generar los ficheros.
   - Esto permite a Django servir estáticos sin Nginx en entornos sencillos.

2. **Configurar Nginx**
   - Para producción se recomienda un reverse proxy Nginx que redirija peticiones HTTPS al contenedor web.
   - Añade un servicio nginx al archivo docker-compose.prod.yml (o un archivo separado para producción) que use una imagen Nginx o un Dockerfile personalizado.
   - En nginx.conf, define un upstream apuntando a la app:
     
nginx
     upstream authorsite {
         server web:8000;
     }

     server {
         listen 80;
         server_name tudominio.com www.tudominio.com;

         location / {
             proxy_pass http://authorsite;
             proxy_set_header Host $host;
             proxy_set_header X-Real-IP $remote_addr;
             proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
             proxy_set_header X-Forwarded-Proto $scheme;
         }
     }

   - Comparte la carpeta de archivos estáticos (staticfiles) entre web y nginx usando un volumen para que Nginx pueda servir los ficheros compilados.

## 7. Activar HTTPS con Let’s Encrypt

1. **Por qué HTTPS**
   - Ejecutar un sitio sin cifrado no es recomendable; los certificados TLS de Let’s Encrypt permiten cifrar tráfico gratuitamente.

2. **Automatización**
   - En desarrollo, usa el entorno de *staging* de Let’s Encrypt para evitar límites de emisión.
   - Existen proyectos como nginx-proxy + acme-companion que gestionan los certificados automáticamente.
   - Alternativamente, instala certbot dentro del servidor y ejecuta:
     
bash
     sudo certbot --nginx -d tudominio.com -d www.tudominio.com

   - Configura redirección 80→443 y actualiza settings.py:
     
python
     SECURE_SSL_REDIRECT = True
     SESSION_COOKIE_SECURE = True
     CSRF_COOKIE_SECURE = True
     SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

   - Actualiza CSRF_TRUSTED_ORIGINS con tus dominios HTTPS.

## 8. Incorporar Sentry para monitoreo de errores

1. **Instalación**
   - Añade la dependencia de Sentry con la integración de Django:
     
bash
     pip install "sentry-sdk[django]"


2. **Configuración en settings.py**
   - Inicializa Sentry con el DSN de tu proyecto en la parte superior del archivo:
     
python
     import sentry_sdk
     sentry_sdk.init(
         dsn=os.getenv("SENTRY_DSN"),
         send_default_pii=True,
         traces_sample_rate=1.0,
         enable_logs=True,
     )


## 9. Añadir una ruta de health check y configurar logs

1. **Endpoint /healthz**
   - Crea una vista simple en views.py que devuelva JsonResponse({"ok": True}) y añade la ruta path("healthz/", views.healthz, name="healthz").
   - Esto permite a servicios externos (como balanceadores o sistemas de orquestación) comprobar si la aplicación está viva.

2. **Logging**
   - Configura un LOGGING dict en settings.py para enviar logs a consola y, opcionalmente, a un archivo.
   - Define nivel de log en WARNING o INFO para producción y asegúrate de que Gunicorn y Celery impriman los logs correctos.

## 10. Limitar abusos y ataques: rate limiting con Nginx

1. **Por qué limitar**
   - Limitar la tasa de peticiones por IP ayuda a prevenir abusos (ataques de fuerza bruta o DDoS).

2. **Configuración básica en Nginx**
   - Define una zona de límites en el bloque http de nginx.conf:
     
nginx
     limit_req_zone $binary_remote_addr zone=mylimit:10m rate=10r/s;

   - Aplica la limitación en el bloque server o location donde desees restringir, por ejemplo:
     
nginx
     server {
         ...
         location /run-etl/ {
             limit_req zone=mylimit burst=20 nodelay;
             proxy_pass http://authorsite;
         }
     }


## 11. Revisión final y pruebas

1. **Revisar la lista de comprobación de despliegue de Django**
   - Elige DEBUG=False, configura SECRET_KEY, ALLOWED_HOSTS y otras variables críticas.
   - Asegúrate de ejecutar python manage.py check --deploy para detectar configuraciones inseguras.

2. **Pruebas locales**
   - Levanta los servicios con docker-compose up --build y accede a http://localhost:8000 para probar la aplicación.
   - Verifica que el ETL se ejecuta en segundo plano (consulta /progress/<job_id>).
   - Asegúrate de que la base de datos Postgres y Redis funcionan correctamente y que la cola Celery responde.

3. **Despliegue en producción**
   - Apunta tu dominio al servidor (DNS).
   - Ejecuta docker-compose -f docker-compose.prod.yml up --build -d para levantar los servicios con Nginx.
   - Obtén y renueva los certificados TLS con Let’s Encrypt.
   - Supervisa los logs y los errores reportados por Sentry para asegurar un funcionamiento estable.

---

Con esta guía, la Persona A podrá preparar un entorno de producción robusto para el proyecto, siguiendo un orden lógico: primero la preparación y configuración de bases de datos y Redis, luego la integración de Celery y la dockerización, después la configuración del reverse proxy con HTTPS, y finalmente el monitoreo, health checks y protección ante abusos.



Pon en el chat este texto con los formatos (me refiero a que no este en formato Markdown, si no que salgan los titulos, las negritas, etc.) 