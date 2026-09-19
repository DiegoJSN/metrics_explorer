from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections


INSTITUTIONS = (
    "Universidad de Zaragoza; Parthenope University of Naples; "
    "Centro de Investigación y Tecnología Agroalimentaria de Aragón; "
    "Hellenic Open University"
)

AUTHORS = [
    (
        1,
        1,
        "Enrique Muñoz-Ulecia",
        INSTITUTIONS,
        "Enrique Muñoz-Ulecia",
        "",
        "0000-0002-7153-7660",
        7,
        5,
        200,
        26,
        23,
        8.0,
        8.4,
    ),
    (
        2,
        2,
        "Diego J. Soler-Navarro",
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "Diego J. Soler-Navarro",
        "DiegoJSN",
        "",
        2,
        0,
        9,
        6,
        6,
        2.0,
        7.1,
    ),
]

WORKS = [
    (
        1, 1, 1, "article", "10.1016/j.agsy.2020.102983",
        "Drivers of change in mountain agriculture: A thirty-year analysis of trajectories of evolution of cattle farming systems in the Spanish Pyrenees",
        "Agricultural Systems",
        "Enrique Muñoz-Ulecia; A. Bernués; I. Casasús; A.M. Olaizola; Sandra Lobón; Daniel Collado",
        "2020", 59, "openalex", 0, 0, INSTITUTIONS, "ES",
    ),
    (
        2, 1, 1, "article", "10.1016/j.envsci.2024.103778",
        "Uneven development and core-periphery dynamics: A journey into the perspective of ecologically unequal exchange",
        "Environmental Science & Policy",
        "Giulio Corsi; Raffaele Guarino; Enrique Muñoz-Ulecia; Alessandro Sapio; Pier Paolo Franzese",
        "2024", 35, "openalex", 0, 0, INSTITUTIONS, "ES",
    ),
    (
        3, 1, 1, "article", "10.1016/j.animal.2023.100748",
        "Evolution of pastoral livestock farming on arid rangelands in the last 15 years",
        "animal",
        "Houda Rjili; Enrique Muñoz-Ulecia; A. Bernués; Mohamed Jaouad; Daniel Collado",
        "2023", 22, "openalex", 0, 0, INSTITUTIONS, "ES",
    ),
    (
        4, 1, 1, "article", "10.1038/s41598-023-41524-4",
        "Dependence on the socio-economic system impairs the sustainability of pasture-based animal agriculture",
        "Scientific Reports",
        "Enrique Muñoz-Ulecia; A. Bernués; Andrei Briones-Hidrovo; I. Casasús; Daniel Collado",
        "2023", 17, "openalex", 0, 0, INSTITUTIONS, "ES",
    ),
    (
        5, 1, 1, "article", "10.1371/journal.pone.0267799",
        "People’s attitudes towards the agrifood system influence the value of ecosystem services of mountain agroecosystems",
        "PLoS ONE",
        "Enrique Muñoz-Ulecia; A. Bernués; Daniel Ondé; Maurizio Ramanzin; Mario Soliño; Enrico Sturaro; Daniel Collado",
        "2022", 10, "openalex", 0, 0, INSTITUTIONS, "ES",
    ),
    (6, 1, 1, "article", None, "Research output 2022 A", "Demo record", "Enrique Muñoz-Ulecia", "2022", 9, "demo", 0, 0, INSTITUTIONS, "ES"),
    (7, 1, 1, "article", None, "Research output 2022 B", "Demo record", "Enrique Muñoz-Ulecia", "2022", 8, "demo", 0, 0, INSTITUTIONS, "ES"),
    (8, 1, 1, "article", None, "Research output 2023 A", "Demo record", "Enrique Muñoz-Ulecia", "2023", 7, "demo", 0, 0, INSTITUTIONS, "ES"),
    (9, 1, 1, "article", None, "Research output 2023 B", "Demo record", "Enrique Muñoz-Ulecia", "2023", 6, "demo", 0, 0, INSTITUTIONS, "ES"),
    (10, 1, 1, "dataset", None, "Dataset 2023", "Demo record", "Enrique Muñoz-Ulecia", "2023", 5, "demo", 0, 0, INSTITUTIONS, "ES"),
    (11, 1, 1, "article", None, "Research output 2024 A", "Demo record", "Enrique Muñoz-Ulecia", "2024", 4, "demo", 0, 0, INSTITUTIONS, "ES"),
    (12, 1, 1, "article", None, "Research output 2024 B", "Demo record", "Enrique Muñoz-Ulecia", "2024", 3, "demo", 0, 0, INSTITUTIONS, "ES"),
    (13, 1, 1, "preprint", None, "Preprint 2024 A", "Demo record", "Enrique Muñoz-Ulecia", "2024", 3, "demo", 0, 0, INSTITUTIONS, "ES"),
    (14, 1, 1, "preprint", None, "Preprint 2024 B", "Demo record", "Enrique Muñoz-Ulecia", "2024", 2, "demo", 0, 0, INSTITUTIONS, "ES"),
    (15, 1, 1, "article", None, "Research output 2025 A", "Demo record", "Enrique Muñoz-Ulecia", "2025", 2, "demo", 0, 0, INSTITUTIONS, "ES"),
    (16, 1, 1, "article", None, "Research output 2025 B", "Demo record", "Enrique Muñoz-Ulecia", "2025", 2, "demo", 0, 0, INSTITUTIONS, "ES"),
    (17, 1, 1, "article", None, "Research output 2025 C", "Demo record", "Enrique Muñoz-Ulecia", "2025", 1, "demo", 0, 0, INSTITUTIONS, "ES"),
    (18, 1, 1, "article", None, "Research output 2025 D", "Demo record", "Enrique Muñoz-Ulecia", "2025", 1, "demo", 0, 0, INSTITUTIONS, "ES"),
    (19, 1, 1, "article", None, "Research output 2025 E", "Demo record", "Enrique Muñoz-Ulecia", "2025", 1, "demo", 0, 0, INSTITUTIONS, "ES"),
    (20, 1, 1, "article", None, "Research output 2025 F", "Demo record", "Enrique Muñoz-Ulecia", "2025", 1, "demo", 0, 0, INSTITUTIONS, "ES"),
    (21, 1, 1, "article", None, "Research output 2025 G", "Demo record", "Enrique Muñoz-Ulecia", "2025", 1, "demo", 0, 0, INSTITUTIONS, "ES"),
    (22, 1, 1, "conference-paper", None, "Conference paper 2025", "Demo record", "Enrique Muñoz-Ulecia", "2025", 1, "demo", 0, 0, INSTITUTIONS, "ES"),
    (23, 1, 1, "preprint", None, "Preprint 2025", "Demo record", "Enrique Muñoz-Ulecia", "2025", 0, "demo", 0, 0, INSTITUTIONS, "ES"),
    (24, 1, 1, "article", None, "Research output 2026", "Demo record", "Enrique Muñoz-Ulecia", "2026", 0, "demo", 0, 0, INSTITUTIONS, "ES"),
    (25, 1, 1, "book-chapter", None, "Book chapter 2026", "Demo record", "Enrique Muñoz-Ulecia", "2026", 0, "demo", 0, 0, INSTITUTIONS, "ES"),
    (26, 1, 1, "paratext", None, "Paratext 2026", "Demo record", "Enrique Muñoz-Ulecia", "2026", 0, "demo", 0, 0, INSTITUTIONS, "ES"),
    (
        101, 2, 2, "article", "10.4000/irpp.3332",
        "Evolution and diversity of institutions: Using institutional grammar to analyze governance changes in traditional crop-livestock systems",
        "International Review of Public Policy",
        "Irene Pérez; Alicia Tenza-Peral; Diego J. Soler-Navarro; Diego Arahuetes-de la Iglesia; Carmen Garate-Marín",
        "2023", 6, "openalex", 0, 0,
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "ES",
    ),
    (
        102, 2, 2, "article", "10.1016/j.jenvman.2025.126004",
        "Environmental strategies increase the resilience of extensive livestock systems to adverse climate conditions",
        "Journal of Environmental Management",
        "Diego J. Soler-Navarro; Alicia Tenza-Peral; Marco A. Janssen; Andrés Giménez; Irene Pérez",
        "2025", 2, "openalex", 0, 0,
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "ES",
    ),
    (
        103, 2, 2, "article", "10.1016/j.ympev.2021.107184",
        "Parallel diversification of the African tree toad genus Nectophryne (Bufonidae)",
        "Molecular Phylogenetics and Evolution",
        "H. Christoph Liedtke; Diego J. Soler-Navarro; Iván Gómez-Mestre; Simon P. Loader; Mark-Oliver Rödel",
        "2021", 1, "openalex", 0, 0,
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "ES",
    ),
    (
        104, 2, 2, "article", "10.3389/conf.fmars.2019.08.00070",
        "Effects of rainfall disturbance on a sandy beach macroinvertebrate community structure and composition in southwestern Spain",
        "Frontiers in Marine Science",
        "F. Javier García-García; Juan Delgado-García; Inés Martínez-Pita; Maria Reyes-Martínez; José Daza; Laura Escalera-González; Diego J. Soler-Navarro",
        "2019", 0, "openalex", 0, 0,
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "ES",
    ),
    (
        105, 2, 2, "preprint", "10.2139/ssrn.5002017",
        "Environmental Strategies Increase the Resilience of Extensive Livestock Systems to Adverse Climate Conditions",
        "SSRN Electronic Journal",
        "Diego J. Soler-Navarro; Andrés Giménez; Irene Pérez; Marco A. Janssen; Alicia Tenza-Peral",
        "2024", 0, "openalex", 0, 0,
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "ES",
    ),
    (
        106, 2, 2, "article", None,
        "Research output 2026",
        "Demo record",
        "Diego J. Soler-Navarro",
        "2026", 0, "demo", 0, 0,
        "Universidad de Zaragoza; Universitat de Miguel Hernández d’Elx; Estación Biológica de Doñana; Arizona State University",
        "ES",
    ),
]

RESOURCES = [
    (
        101, 2, 2, "software", None,
        "https://github.com/DiegoJSN/sequiabasaltonetlogo",
        "sequiabasaltonetlogo", "DiegoJSN", "2022", None, "github",
    ),
    (
        102, 2, 2, "software", None,
        "https://github.com/DiegoJSN/soslivestock",
        "soslivestock", "DiegoJSN; MarcoAJanssen", "2023", None, "github",
    ),
    (
        103, 2, 2, "software", None,
        "https://github.com/DiegoJSN/syst_rev_tool",
        "syst_rev_tool", "DiegoJSN", "2026", None, "github",
    ),
]


class Command(BaseCommand):
    help = "Create deterministic portfolio data derived from the documented example."

    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            self.stdout.write("DEMO_MODE is disabled; nothing was changed.")
            return

        with connections["authors"].cursor() as cursor:
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS author_summary (
                    id INTEGER PRIMARY KEY, job_id INTEGER, name TEXT, institution TEXT,
                    zenodo_name TEXT, github_name TEXT, orcid TEXT, h_index INTEGER,
                    i10_index INTEGER, n_citas INTEGER, n_publicaciones INTEGER,
                    n_publicaciones_oa INTEGER, publicaciones_citas_avrg REAL,
                    profile_load_time_seconds REAL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS works (
                    id INTEGER PRIMARY KEY, job_id INTEGER, author_id INTEGER, type TEXT,
                    doi TEXT, title TEXT, published_in TEXT, authors TEXT, year TEXT,
                    cited_by INTEGER, api_source TEXT, wikipedia_mentions INTEGER,
                    newsfeed_mentions INTEGER, authors_institutions TEXT,
                    institutions_country_code TEXT
                );
                CREATE TABLE IF NOT EXISTS zenodo_github (
                    id INTEGER PRIMARY KEY, job_id INTEGER, author_id INTEGER, type TEXT,
                    doi TEXT, github_url TEXT, title TEXT, authors TEXT, year TEXT,
                    cited_by INTEGER, api_source TEXT
                );
                """
            )
            cursor.execute("DELETE FROM zenodo_github")
            cursor.execute("DELETE FROM works")
            cursor.execute("DELETE FROM author_summary")
            cursor.executemany(
                """INSERT INTO author_summary
                (id, job_id, name, institution, zenodo_name, github_name, orcid,
                 h_index, i10_index, n_citas, n_publicaciones, n_publicaciones_oa,
                 publicaciones_citas_avrg, profile_load_time_seconds)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                AUTHORS,
            )
            cursor.executemany(
                """INSERT INTO works
                (id, job_id, author_id, type, doi, title, published_in, authors, year,
                 cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                 authors_institutions, institutions_country_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                WORKS,
            )
            cursor.executemany(
                """INSERT INTO zenodo_github
                (id, job_id, author_id, type, doi, github_url, title, authors, year,
                 cited_by, api_source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                RESOURCES,
            )

        self.stdout.write(self.style.SUCCESS("Portfolio demo data is ready."))
