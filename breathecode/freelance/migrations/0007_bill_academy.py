# Generated by Django 3.1.6 on 2021-02-25 01:42

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('admissions', '0014_auto_20201218_0534'),
        ('freelance', '0006_auto_20200728_2225'),
    ]

    operations = [
        migrations.AddField(
            model_name='bill',
            name='academy',
            field=models.ForeignKey(default=None, null=True, on_delete=django.db.models.deletion.CASCADE, to='admissions.academy'),
        ),
    ]
