from django.db import migrations, models


def dedupe_order_numbers(apps, schema_editor):
    Order = apps.get_model('server', 'Order')

    # Keep track of values we assign in this run
    seen = set(
        Order.objects.exclude(order_number__isnull=True)
                     .exclude(order_number__exact='')
                     .values_list('order_number', flat=True)
    )

    # Iterate oldest to newest for deterministic results
    for o in Order.objects.all().order_by('id').only('id', 'order_number'):
        num = (o.order_number or '').strip()
        if not num or num in seen:
            base = f"MM{o.id:08d}"
            candidate = base
            k = 1
            while Order.objects.filter(order_number=candidate).exclude(pk=o.pk).exists() or candidate in seen:
                k += 1
                candidate = f"{base}-{k}"
            o.order_number = candidate
            o.save(update_fields=['order_number'])
            seen.add(candidate)
        else:
            seen.add(num)


class Migration(migrations.Migration):

    dependencies = [
        ('server', '0022_remove_order_uniq_invoice_number_nonempty_and_more'),
    ]

    operations = [
        migrations.RunPython(dedupe_order_numbers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='order',
            name='order_number',
            field=models.CharField(max_length=20, unique=True),
        ),
    ]
