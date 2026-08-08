from django.db import migrations, models

import django.core.validators


def _profile_report_envelope(profile):
    envelope = (
        profile.verification_report_object_key,
        profile.verification_report_object_version,
        profile.verification_report_hash,
    )
    if (
        not all(isinstance(value, str) and value for value in envelope)
        or len(envelope[2]) != 64
        or any(character not in "0123456789abcdef" for character in envelope[2])
    ):
        raise RuntimeError(
            "Existing extraction profile decision has no complete frozen report envelope"
        )
    return envelope


def backfill_profile_report_envelopes(apps, schema_editor):
    """Bind legacy decision chains to their current immutable profile report projection."""
    decision_model = apps.get_model("evidence", "ExtractionProfileDecision")
    alias = schema_editor.connection.alias
    decisions = (
        decision_model.objects.using(alias)
        .select_related("profile_snapshot")
        .iterator()
    )
    for decision in decisions:
        object_key, object_version, report_hash = _profile_report_envelope(
            decision.profile_snapshot
        )
        decision.verification_report_object_key = object_key
        decision.verification_report_object_version = object_version
        decision.verification_report_hash = report_hash
        decision.save(
            using=alias,
            update_fields=(
                "verification_report_object_key",
                "verification_report_object_version",
                "verification_report_hash",
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("evidence", "0003_evidenceasset_raw_input_fingerprint"),
    ]

    operations = [
        migrations.AddField(
            model_name="extractionprofiledecision",
            name="verification_report_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[a-f0-9]{64}$",
                        "Expected a lowercase SHA-256 digest",
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="extractionprofiledecision",
            name="verification_report_object_key",
            field=models.CharField(blank=True, max_length=1024, null=True),
        ),
        migrations.AddField(
            model_name="extractionprofiledecision",
            name="verification_report_object_version",
            field=models.CharField(blank=True, max_length=256, null=True),
        ),
        migrations.RunPython(
            backfill_profile_report_envelopes,
            migrations.RunPython.noop,
        ),
    ]
