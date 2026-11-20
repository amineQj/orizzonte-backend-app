from django.db import migrations


def forwards(apps, schema_editor):
    PricingConfig = apps.get_model("missions", "PricingConfig")
    if PricingConfig.objects.exists():
        return
    from missions.pricing_services import seed_default_config

    seed_default_config(activate=True)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("missions", "0009_mission_uniq_active_driver_mission"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]