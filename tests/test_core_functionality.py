import os
import tempfile
import unittest
from unittest.mock import patch

# Импортируем функции из твоего основного файла checker.py
from checker import get_xray_download_url, setup_xray_bin, convert_link_via_xray


class TestXrayIntegration(unittest.TestCase):

    def setUp(self):
        """
        Создаем изолированное окружение для каждого теста.
        Подменяем рабочую директорию xray_bin на временную системную папку.
        """
        self.tmp_dir = tempfile.TemporaryDirectory()
        
        # Патчим переменную XRAY_BIN_DIR внутри checker.py, чтобы скачивание шло в изолированное место
        self.patcher = patch("checker.XRAY_BIN_DIR", self.tmp_dir.name)
        self.patcher.start()

    def tearDown(self):
        """Очищаем за собой скачанные файлы после завершения теста."""
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def test_01_real_github_api_url(self):
        """Интеграционный тест: делаем реальный запрос к GitHub API и проверяем URL."""
        if os.getenv("RUN_XRAY_INTEGRATION_TESTS") != "1":
            self.skipTest("Set RUN_XRAY_INTEGRATION_TESTS=1 to run network integration tests")

        print("\n[RUN] Запрос к реальному API GitHub...")
        url = get_xray_download_url()
        
        print(f"[INFO] Ссылка на актуальный релиз получена: {url}")
        
        self.assertIsNotNone(url, "URL для скачивания Xray не должен быть None")
        self.assertTrue(
            url.startswith("https://github.com/XTLS/Xray-core/releases/download/"), 
            f"Получен некорректный URL: {url}"
        )
        self.assertTrue(
            url.endswith((".zip", ".tar.gz", ".tgz")), 
            f"Ссылка ведет не на архив. Получено: {url}"
        )

    def test_02_real_download_and_extract(self):
        """Интеграционный тест: реально скачиваем, распаковываем ядро Xray и проверяем его свойства."""
        if os.getenv("RUN_XRAY_INTEGRATION_TESTS") != "1":
            self.skipTest("Set RUN_XRAY_INTEGRATION_TESTS=1 to run network integration tests")

        print("\n[RUN] Скачивание и распаковка реального ядра Xray...")
        xray_path = setup_xray_bin()
        
        print(f"[INFO] Бинарник Xray успешно развернут по пути: {xray_path}")

        # 1. Проверяем, что файл физически существует на диске
        self.assertTrue(os.path.exists(xray_path), f"Файл не найден по пути: {xray_path}")
        self.assertTrue(os.path.isfile(xray_path), f"Путь указывает не на файл: {xray_path}")
        
        # 2. Проверяем, что у файла есть права на исполнение (chmod +x отработал)
        self.assertTrue(os.access(xray_path, os.X_OK), f"У файла {xray_path} отсутствуют права на запуск!")

    def test_03_vless_reality_parse_check(self):
        """Проверяем встроенный парсер VLESS Reality ссылки."""
        print("\n[RUN] Проверка встроенного парсера VLESS Reality...")

        # Тестовая VLESS ссылка с Reality
        test_link = (
            "vless://70440b40-5e3b-41c1-a9cb-d7113d216ef8@demw.verymad.net:27439"
            "?type=tcp&security=reality&pbk=Re0aWbYgk775QZ2eiIzh9fwPANq8HfLlrp9vbTC8vXQ"
            "&sid=ac2ceae11eb0&sni=www.vk.com#MySuperNode"
        )
        
        result = convert_link_via_xray(test_link)
        
        self.assertIsNotNone(result, "convert_link_via_xray вернул None")
        remark, outbound = result
        
        print(f"[INFO] Название ноды успешно извлечено: {remark}")
        self.assertEqual(remark, "MySuperNode")
        self.assertEqual(outbound.get("protocol"), "vless")
        self.assertIn("streamSettings", outbound)
        self.assertEqual(outbound["streamSettings"].get("security"), "reality")
        self.assertEqual(outbound["streamSettings"]["realitySettings"].get("publicKey"), "Re0aWbYgk775QZ2eiIzh9fwPANq8HfLlrp9vbTC8vXQ")

    def test_04_vless_xhttp_parse_check(self):
        """Проверяем встроенный парсер VLESS XHTTP ссылки."""
        print("\n[RUN] Проверка встроенного парсера VLESS XHTTP...")

        test_link = (
            "vless://70440b40-5e3b-41c1-a9cb-d7113d216ef8@example.com:443"
            "?type=xhttp&security=reality&sni=example.com&fp=chrome&pbk=testPublicKey&sid=abcd"
            "&host=cdn.example.com&path=%2Fxhttp&mode=auto"
            "&extra=%7B%22headers%22%3A%7B%22X-Test%22%3A%22ok%22%7D%7D#XHTTPNode"
        )

        result = convert_link_via_xray(test_link)

        self.assertIsNotNone(result, "convert_link_via_xray вернул None")
        remark, outbound = result
        stream_settings = outbound["streamSettings"]
        xhttp_settings = stream_settings["xhttpSettings"]

        self.assertEqual(remark, "XHTTPNode")
        self.assertEqual(stream_settings.get("network"), "xhttp")
        self.assertEqual(stream_settings.get("security"), "reality")
        self.assertEqual(xhttp_settings.get("host"), "cdn.example.com")
        self.assertEqual(xhttp_settings.get("path"), "/xhttp")
        self.assertEqual(xhttp_settings.get("mode"), "auto")
        self.assertEqual(xhttp_settings["extra"]["headers"]["X-Test"], "ok")


if __name__ == "__main__":
    unittest.main()
