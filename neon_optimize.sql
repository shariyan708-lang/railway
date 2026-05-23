-- Optional Neon PostgreSQL optimization script.
-- Run this once in Neon SQL Editor after the bot has created its tables.

CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);
CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance_cents);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);
CREATE INDEX IF NOT EXISTS idx_users_banned_created ON users(is_banned, created_at);

CREATE INDEX IF NOT EXISTS idx_channels_enabled_sort ON channels(enabled, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_products_active_sort ON products(active, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_variants_product_active_sort ON product_variants(product_id, active, sort_order, id);

CREATE INDEX IF NOT EXISTS idx_stock_variant_status_id ON stock_items(variant_id, status, id);
CREATE INDEX IF NOT EXISTS idx_stock_sold_to ON stock_items(sold_to);

CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_topups_status_created ON topups(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_redeem_codes_active ON redeem_codes(active, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_redeem_claims_user ON redeem_claims(user_id, created_at DESC);

ANALYZE users;
ANALYZE channels;
ANALYZE products;
ANALYZE product_variants;
ANALYZE stock_items;
ANALYZE orders;
ANALYZE topups;
ANALYZE redeem_codes;
ANALYZE redeem_claims;
