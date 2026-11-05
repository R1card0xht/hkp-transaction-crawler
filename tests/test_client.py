from __future__ import annotations

import unittest

from hkp_transaction_crawler.client import HKPClient


class ClientTests(unittest.TestCase):
    def test_browser_copied_chinese_path_is_encoded_once(self) -> None:
        raw = "https://www.hkp.com.hk/zh-cn/transaction/九龙-天晋-I123"
        encoded = HKPClient._normalise_url(raw)
        self.assertIn("%E4%B9%9D%E9%BE%99", encoded)
        self.assertEqual(HKPClient._normalise_url(encoded), encoded)

