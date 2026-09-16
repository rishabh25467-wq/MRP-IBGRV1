import os
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv('/app/backend/.env')
client = MongoClient(os.environ['MONGO_URL'])
db = client[os.environ['DB_NAME']]

doc = {
    '_id': 'TESTMANUALGI3',
    'status': 'created_in_sap',
    'created_by': 'STO Manual GI Tester',
    'created_by_user_id': 'test-tid:sto-manual-gi-oid',
    'created_at': datetime.now(timezone.utc),
    'ship_from_site_id': 'P1',
    'ship_to_site_id': 'P2',
    'ship_to_location_id': 'P2-RM',
    'ship_to_location_name': 'P2-RM',
    'delivery_priority': 'Immediate',
    'requested_delivery_date': '2026-09-25',
    'items': [{'line_no': 1, 'product_id': 'TESTPRODMGI4', 'description': 'Test Product',
               'source_warehouse_id': 'P1-RM', 'source_warehouse_name': 'P1-RM',
               'available_qty': 100, 'requested_qty': 1, 'unit_of_measure': 'EA',
               'availability_status': 'Available'}],
    'sap_order_id': '999997',
    'sap_order_uuid': 'test-uuid-999997',
    'gi_status': 'awaiting_manual_gi',
    'gi_error': None,
    'gi_job_running': False,
    'gi_delivery_request_id': 'TESTDR-999997',
    'outbound_delivery_ids': [],
    'erp_portal_status': None,
}
db['stock_transfer_orders'].replace_one({'_id': 'TESTMANUALGI3'}, doc, upsert=True)
print('Seeded TESTMANUALGI3')
print(db['stock_transfer_orders'].find_one({'_id': 'TESTMANUALGI3'}))
