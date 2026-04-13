# test_url_builder.py

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.url_builder import build_listings_url; 
print(build_listings_url('prodazhbi', 'oblast-blagoevgrad')); 
print(build_listings_url('prodazhbi', 'oblast-blagoevgrad', page=2)); 
print(build_listings_url('naemi', 'grad-sofia', page=5))