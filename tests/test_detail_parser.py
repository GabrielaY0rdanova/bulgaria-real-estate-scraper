# test_detail_parser.py

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.fetcher import fetch_page
from scraper.detail_parser import parse_detail_page

url = "https://www.imot.bg/obiava-1b175838673895492-prodava-dvustaen-apartament-grad-pernik-tsentar"

html = fetch_page(url)

if html:
    result = parse_detail_page(html)
    print(result)
else:
    print("Failed to fetch page.")