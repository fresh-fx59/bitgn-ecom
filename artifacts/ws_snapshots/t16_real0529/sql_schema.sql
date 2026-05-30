sql
"CREATE TABLE product_categories (
	product_category_id TEXT PRIMARY KEY,
	product_category_name TEXT NOT NULL,
	product_department TEXT NOT NULL
)"
"CREATE TABLE product_kinds (
	product_kind_id TEXT PRIMARY KEY,
	product_category_id TEXT NOT NULL,
	product_kind_name TEXT NOT NULL
)"
"CREATE TABLE product_families (
	product_family_id TEXT PRIMARY KEY,
	product_category_id TEXT NOT NULL,
	product_kind_id TEXT NOT NULL,
	brand TEXT NOT NULL,
	series TEXT NOT NULL,
	model TEXT NOT NULL,
	product_family_name TEXT NOT NULL,
	properties TEXT NOT NULL
)"
"CREATE TABLE product_variants (
	product_sku TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	product_category_id TEXT NOT NULL,
	product_kind_id TEXT NOT NULL,
	product_family_id TEXT NOT NULL,
	brand TEXT NOT NULL,
	series TEXT NOT NULL,
	model TEXT NOT NULL,
	product_name TEXT NOT NULL,
	price_cents INTEGER NOT NULL,
	price_currency TEXT NOT NULL,
	properties TEXT NOT NULL
)"
"CREATE TABLE product_variant_properties (
	product_sku TEXT NOT NULL,
	property_key TEXT NOT NULL,
	property_value_text TEXT NOT NULL,
	property_value_number REAL,
	PRIMARY KEY (product_sku, property_key)
)"
"CREATE TABLE stores (
	store_id TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	store_name TEXT NOT NULL,
	city TEXT NOT NULL,
	is_open INTEGER NOT NULL,
	latitude REAL NOT NULL,
	longitude REAL NOT NULL
)"
"CREATE TABLE store_inventory (
	store_id TEXT NOT NULL,
	product_sku TEXT NOT NULL,
	on_hand_quantity INTEGER NOT NULL,
	reserved_quantity INTEGER NOT NULL,
	available_today_quantity INTEGER NOT NULL,
	incoming_quantity INTEGER NOT NULL,
	next_arrival_in_days INTEGER,
	PRIMARY KEY (store_id, product_sku),
	FOREIGN KEY (store_id) REFERENCES stores(store_id),
	FOREIGN KEY (product_sku) REFERENCES product_variants(product_sku)
)"
"CREATE TABLE customer_accounts (
	customer_id TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	customer_display_name TEXT NOT NULL,
	customer_email TEXT NOT NULL,
	home_city TEXT NOT NULL,
	home_latitude REAL NOT NULL,
	home_longitude REAL NOT NULL
)"
"CREATE TABLE employee_accounts (
	employee_id TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	employee_display_name TEXT NOT NULL,
	employee_email TEXT NOT NULL,
	job_title TEXT NOT NULL,
	store_id TEXT NOT NULL,
	FOREIGN KEY (store_id) REFERENCES stores(store_id)
)"
"CREATE TABLE employee_role_assignments (
	employee_id TEXT NOT NULL,
	role_code TEXT NOT NULL,
	PRIMARY KEY (employee_id, role_code),
	FOREIGN KEY (employee_id) REFERENCES employee_accounts(employee_id)
)"
"CREATE TABLE shopping_baskets (
	basket_id TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	customer_id TEXT NOT NULL,
	store_id TEXT NOT NULL,
	basket_status TEXT NOT NULL,
	basket_created_at TEXT NOT NULL,
	discount_percent INTEGER,
	discount_reason_code TEXT,
	discount_issuer_employee_id TEXT,
	FOREIGN KEY (customer_id) REFERENCES customer_accounts(customer_id),
	FOREIGN KEY (store_id) REFERENCES stores(store_id)
)"
"CREATE TABLE shopping_basket_items (
	basket_id TEXT NOT NULL,
	line_number INTEGER NOT NULL,
	product_sku TEXT NOT NULL,
	requested_quantity INTEGER NOT NULL,
	PRIMARY KEY (basket_id, line_number),
	FOREIGN KEY (basket_id) REFERENCES shopping_baskets(basket_id),
	FOREIGN KEY (product_sku) REFERENCES product_variants(product_sku)
)"
"CREATE TABLE payment_transactions (
	payment_id TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	basket_id TEXT NOT NULL,
	is_archived_basket_reference INTEGER NOT NULL,
	customer_id TEXT NOT NULL,
	store_id TEXT NOT NULL,
	payment_amount_cents INTEGER NOT NULL,
	payment_currency TEXT NOT NULL,
	payment_status TEXT NOT NULL,
	payment_created_at TEXT NOT NULL,
	payment_method_fingerprint TEXT NOT NULL,
	device_fingerprint TEXT NOT NULL,
	observed_latitude REAL NOT NULL,
	observed_longitude REAL NOT NULL,
	three_ds_status TEXT,
	three_ds_failure_reason TEXT,
	three_ds_attempts INTEGER,
	three_ds_max_attempts INTEGER,
	FOREIGN KEY (customer_id) REFERENCES customer_accounts(customer_id),
	FOREIGN KEY (store_id) REFERENCES stores(store_id)
)"
"CREATE TABLE payment_transaction_items (
	payment_id TEXT NOT NULL,
	line_number INTEGER NOT NULL,
	product_sku TEXT NOT NULL,
	purchased_quantity INTEGER NOT NULL,
	item_unit_price_cents INTEGER NOT NULL,
	PRIMARY KEY (payment_id, line_number),
	FOREIGN KEY (payment_id) REFERENCES payment_transactions(payment_id),
	FOREIGN KEY (product_sku) REFERENCES product_variants(product_sku)
)"
"CREATE TABLE return_requests (
	return_id TEXT PRIMARY KEY,
	record_path TEXT NOT NULL,
	basket_id TEXT NOT NULL,
	customer_id TEXT NOT NULL,
	payment_id TEXT NOT NULL,
	return_status TEXT NOT NULL,
	return_reason_code TEXT NOT NULL,
	return_created_at TEXT NOT NULL,
	FOREIGN KEY (basket_id) REFERENCES shopping_baskets(basket_id),
	FOREIGN KEY (customer_id) REFERENCES customer_accounts(customer_id),
	FOREIGN KEY (payment_id) REFERENCES payment_transactions(payment_id)
)"
