from django.db import migrations

ADD_FK_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name='server_coupon'
    ) THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='server_order_coupon_id_fk'
        ) THEN
            ALTER TABLE server_order
            ADD CONSTRAINT server_order_coupon_id_fk
            FOREIGN KEY (coupon_id) REFERENCES server_coupon (id)
            DEFERRABLE INITIALLY DEFERRED;
        END IF;
    END IF;
END$$;
"""

class Migration(migrations.Migration):
    dependencies = [("server", "0003_add_coupon_fk")]
    operations = [migrations.RunSQL(ADD_FK_SQL)]
