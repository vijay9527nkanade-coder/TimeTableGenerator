from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='timetableentry',
            name='is_pinned',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Pinned entries are manually fixed and preserved during timetable regeneration. '
                    'The solver schedules all other lectures around them.'
                )
            ),
        ),
    ]
