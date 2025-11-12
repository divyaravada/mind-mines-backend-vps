from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ("server", "0004_order_stock_locked"),  # last one you showed
    ]

    operations = [
        migrations.RunSQL(
            """
            ALTER TABLE server_coupon_products
              ADD COLUMN IF NOT EXISTS id BIGSERIAL;
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'server_coupon_products_pkey'
              ) THEN
                ALTER TABLE server_coupon_products
                  ADD CONSTRAINT server_coupon_products_pkey PRIMARY KEY (id);
              END IF;
            END$$;
            """,
            reverse_sql="",
        ),
        migrations.RunSQL(
            """
            -- de-dup before unique constraint
            WITH dups AS (
              SELECT ctid, ROW_NUMBER() OVER (
                PARTITION BY coupon_id, product_id ORDER BY ctid
              ) AS rn
              FROM server_coupon_products
            )
            DELETE FROM server_coupon_products
            WHERE ctid IN (SELECT ctid FROM dups WHERE rn > 1);

            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uniq_server_coupon_products_pair'
              ) THEN
                ALTER TABLE server_coupon_products
                  ADD CONSTRAINT uniq_server_coupon_products_pair
                  UNIQUE (coupon_id, product_id);
              END IF;
            END$$;
            """,
            reverse_sql="",
        ),
    ]
