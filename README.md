# Metrics Explorer — Researcher Tool

> **Portfolio / Demo Version** — esta rama permite explorar el proyecto sin credenciales ni una instalación compleja.

## Descripción

Aplicación web que reúne en un perfil indicadores sobre la actividad de un investigador: publicaciones, citas, índice h, acceso abierto, instituciones, proyectos de GitHub, registros de Zenodo y menciones externas.

La versión completa busca investigadores, consulta distintas APIs, ejecuta un proceso ETL y presenta los resultados mediante paneles y gráficos.

La aplicación estuvo disponible públicamente durante siete meses en [metricsexplorer.metricsschool.com/es/](https://metricsexplorer.metricsschool.com/es/) y ahora se encuentra disponible en GitHub.

## Cómo probarlo

### 1. Ejecutable para Windows — recomendado

Esta es la forma más sencilla de probar el flujo completo: ejecuta el ETL desde el ordenador sin instalar Python, PostgreSQL, Redis ni Docker.

1. Abre la [última versión publicada](https://github.com/MetricsSchool/researcher_tool/releases/latest).
2. En **Assets**, descarga `MetricsExplorer-Demo-Windows.zip`.
3. Haz clic derecho en el ZIP y selecciona **Extraer todo**.
4. Abre la carpeta extraída y ejecuta `MetricsExplorer-Demo.exe`.
5. Mantén abierta la ventana negra. La aplicación abrirá automáticamente el navegador.
6. Introduce un nombre u ORCID —o utiliza uno de los ejemplos— y pulsa **Analizar**.

La búsqueda en vivo necesita conexión a Internet. Las APIs pueden aplicar límites o no disponer de todos los datos. GitHub y Zenodo funcionan sin tokens, aunque con límites más bajos.

### 2. Demo online — visita rápida

[**Abrir la demo online**](https://researcher-tool-demo.onrender.com/es/)

Elige **Usar ejemplo con ORCID** o **Usar ejemplo con Nombre + GitHub**. Después pulsa **Analizar**. Render utiliza datos precargados y no consulta servicios externos.

## Qué puedes probar

- Cargar ejemplos con un clic o buscar investigadores por nombre u ORCID desde el ejecutable.
- Seleccionar candidatos cuando existen coincidencias.
- Seguir el progreso del procesamiento.
- Consultar publicaciones, citas e indicadores.
- Ver la evolución temporal y los trabajos destacados.
- Explorar repositorios y datasets.

## Tecnología y funcionamiento

- **Python y Django:** aplicación web y lógica principal.
- **PostgreSQL:** almacenamiento de la versión completa.
- **Celery y Redis:** procesamiento asíncrono en producción.
- **OpenAlex, GitHub, Zenodo, OpenAIRE y DOI:** fuentes externas.
- **Plotly, Bootstrap y Tailwind CSS:** gráficos e interfaz.
- **SQLite y Waitress:** ejecución local portable.
- **Docker, Gunicorn, Nginx y Caddy:** empaquetado y despliegue.
- **PyInstaller:** construcción del ejecutable de Windows.

## Diferencias de la rama demo

La demo online utiliza dos instantáneas precargadas —ORCID y Nombre + GitHub— para garantizar una experiencia inmediata y fiable dentro de las limitaciones del servidor gratuito de Render. El ejecutable activa el procesamiento local y puede recorrer el flujo real sin utilizar credenciales personales. Tanto Render como el ejecutable presentan la interfaz únicamente en español. La rama `main` conserva la infraestructura completa con PostgreSQL, Celery y Redis.

## Limitaciones y privacidad

Los indicadores precargados representan una instantánea demostrativa y pueden no coincidir con los valores actuales de las fuentes originales. Los resultados en vivo dependen de la información pública disponible en servicios externos y no deben utilizarse para evaluar personas. No se incluyen tokens ni contraseñas en el repositorio.

---

Esta rama está preparada específicamente como **versión demostrativa para portfolio profesional**.
