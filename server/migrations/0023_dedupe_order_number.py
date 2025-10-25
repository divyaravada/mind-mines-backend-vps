from django.db import migrations

def dedupe_order_numbers(apps, schema_editor):
    Order = apps.get_model('server', 'Order')
    # Build a set of order_numbers we’ve already seen
    seen = set()
    # Iterate oldest to newest for predictable results
    for o in Order.objects.all().order_by('id').only('id', 'order_number'):
        num = (o.order_number or '').strip()
        if not num or num in seen:
            # Assign a deterministic unique fallback (e.g., MM00001234)
            new_num = f"MM{o.id:08d}"
            # If even that somehow collides, keep adding a suffix
            suffix = 1
            candidate = new_num
            while Order.objects.filter(order_number=candidate).exclude(pk=o.pk).exists():
                suffix += 1
                candidate = f"{new_num}-{suffix}"
            o.order_number = candidate
            o.save(update_fields=['order_number'])
            seen.add(o.order_number)
        else:
            seen.add(num)

class Migration(migrations.Migration):

    dependencies = [
        ('server', '0022_remove_order_uniq_invoice_number_nonempty_and_more'),  # update to your real dependency
    ]

    operations = [
        migrations.RunPython(dedupe_order_numbers, migrations.RunPython.noop),
    ]
