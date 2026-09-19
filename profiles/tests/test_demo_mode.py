from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.test import Client, TestCase, override_settings

from profiles.runner import _ensure_author_summary_exists


@override_settings(DEMO_MODE=True, LOCAL_ETL_MODE=False)
class DemoModeTests(TestCase):
    databases = {"default", "authors"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_demo", verbosity=0)

    def test_seed_matches_documented_orcid_profile(self):
        with connections["authors"].cursor() as cursor:
            cursor.execute(
                """
                SELECT name, orcid, h_index, i10_index, n_citas,
                       n_publicaciones, n_publicaciones_oa,
                       publicaciones_citas_avrg
                FROM author_summary
                WHERE id = 1
                """
            )
            self.assertEqual(
                cursor.fetchone(),
                (
                    "Enrique Muñoz-Ulecia",
                    "0000-0002-7153-7660",
                    7,
                    5,
                    200,
                    26,
                    23,
                    8.0,
                ),
            )
            cursor.execute(
                "SELECT COUNT(*), SUM(cited_by) FROM works WHERE author_id = 1"
            )
            self.assertEqual(cursor.fetchone(), (26, 200))

    def test_seed_matches_documented_name_github_profile(self):
        with connections["authors"].cursor() as cursor:
            cursor.execute(
                """
                SELECT name, orcid, github_name, h_index, i10_index, n_citas,
                       n_publicaciones, n_publicaciones_oa,
                       publicaciones_citas_avrg
                FROM author_summary
                WHERE id = 2
                """
            )
            self.assertEqual(
                cursor.fetchone(),
                (
                    "Diego J. Soler-Navarro",
                    "",
                    "DiegoJSN",
                    2,
                    0,
                    9,
                    6,
                    6,
                    2.0,
                ),
            )
            cursor.execute(
                "SELECT COUNT(*), SUM(cited_by) FROM works WHERE author_id = 2"
            )
            self.assertEqual(cursor.fetchone(), (6, 9))
            cursor.execute(
                "SELECT COUNT(*) FROM zenodo_github WHERE author_id = 2"
            )
            self.assertEqual(cursor.fetchone()[0], 3)

    def test_health_does_not_require_redis(self):
        response = Client().get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["db"])
        self.assertTrue(response.json()["redis"])

    def test_demo_route_redirects_to_seeded_profile(self):
        response = Client().get("/es/demo/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("author_id=1", response["Location"])

    def test_search_page_shows_both_example_buttons(self):
        response = Client().get("/es/")
        self.assertContains(response, 'id="use-orcid-example"')
        self.assertContains(response, 'id="use-name-example"')
        self.assertContains(response, "Descargar ejecutable")
        self.assertContains(response, "limitaciones de uso del servidor gratuito de Render")
        self.assertNotContains(response, "O abrir directamente el perfil de demostración")
        self.assertNotContains(response, 'id="language-select"')

    def test_demo_interface_is_spanish_only(self):
        self.assertEqual(settings.LANGUAGE_CODE, "es")
        self.assertEqual([code for code, _ in settings.LANGUAGES], ["es"])

    def test_online_example_submit_loads_enrique_profile(self):
        response = Client().post(
            "/es/",
            {
                "orcid": "0000-0002-7153-7660",
                "researcher_name": "",
                "github_name": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enrique Muñoz-Ulecia")
        self.assertContains(response, "0000-0002-7153-7660")
        self.assertContains(response, "Drivers of change in mountain agriculture")

    def test_online_name_github_example_loads_diego_profile(self):
        response = Client().post(
            "/es/",
            {
                "orcid": "",
                "researcher_name": "Diego J. Soler Navarro",
                "github_name": "DiegoJSN",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diego J. Soler-Navarro")
        self.assertContains(response, "Evolution and diversity of institutions")
        self.assertContains(response, "https://github.com/DiegoJSN/syst_rev_tool")


@override_settings(DEMO_MODE=True, LOCAL_ETL_MODE=True)
class LocalEtlBootstrapTests(TestCase):
    databases = {"default", "authors"}

    def test_first_run_creates_empty_author_table_on_sqlite(self):
        with connections["authors"].cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS author_summary")
        _ensure_author_summary_exists()
        with connections["authors"].cursor() as cursor:
            cursor.execute("PRAGMA table_info(author_summary)")
            columns = {row[1] for row in cursor.fetchall()}
        self.assertIn("job_id", columns)
        self.assertIn("profile_load_time_seconds", columns)
