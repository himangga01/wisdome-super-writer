import django.db.models.deletion
import uuid
from django.db import migrations, models


def install_editorial_immutability_guards(apps, schema_editor):
    verification_table = schema_editor.quote_name(
        "editorial_eventclusterverification"
    )
    membership_table = schema_editor.quote_name(
        "editorial_articleeventcluster"
    )
    vendor = schema_editor.connection.vendor
    with schema_editor.connection.cursor() as cursor:
        if vendor == "postgresql":
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION
                    editorial_guard_event_cluster_verification()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION
                        'EventClusterVerification is append-only'
                        USING ERRCODE = '23514';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER editorial_event_verification_append_only
                BEFORE UPDATE OR DELETE ON {verification_table}
                FOR EACH ROW EXECUTE FUNCTION
                    editorial_guard_event_cluster_verification();
                """
            )
        if vendor == "sqlite":
            cursor.execute(
                f"""
                CREATE TRIGGER editorial_event_verification_no_update
                BEFORE UPDATE ON {verification_table}
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'EventClusterVerification is append-only'
                    );
                END;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER editorial_event_verification_no_delete
                BEFORE DELETE ON {verification_table}
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'EventClusterVerification is append-only'
                    );
                END;
                """
            )
        elif vendor != "postgresql":
            raise RuntimeError(
                "Editorial immutability guards do not support "
                f"database vendor {vendor!r}"
            )
        if vendor == "postgresql":
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION
                    editorial_guard_article_event_cluster()
                RETURNS trigger AS $$
                BEGIN
                    IF TG_OP <> 'INSERT' THEN
                        RAISE EXCEPTION
                            'ArticleEventCluster is append-only'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                        FROM editorial_eventclusterverification verification
                        WHERE verification.id = NEW.verification_id
                          AND verification.cluster_id = NEW.event_cluster_id
                          AND verification.evidence_manifest_hash =
                              NEW.cluster_snapshot_hash
                    ) THEN
                        RAISE EXCEPTION
                            'ArticleEventCluster verification mismatch'
                            USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER editorial_article_event_cluster_guard
                BEFORE INSERT OR UPDATE OR DELETE ON {membership_table}
                FOR EACH ROW EXECUTE FUNCTION
                    editorial_guard_article_event_cluster();
                """
            )
            return
        cursor.execute(
            f"""
            CREATE TRIGGER editorial_article_event_cluster_no_update
            BEFORE UPDATE ON {membership_table}
            BEGIN
                SELECT RAISE(ABORT, 'ArticleEventCluster is append-only');
            END;
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER editorial_article_event_cluster_no_delete
            BEFORE DELETE ON {membership_table}
            BEGIN
                SELECT RAISE(ABORT, 'ArticleEventCluster is append-only');
            END;
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER editorial_article_event_cluster_insert_guard
            BEFORE INSERT ON {membership_table}
            WHEN NOT EXISTS (
                SELECT 1
                FROM editorial_eventclusterverification verification
                WHERE verification.id = NEW.verification_id
                  AND verification.cluster_id = NEW.event_cluster_id
                  AND verification.evidence_manifest_hash =
                      NEW.cluster_snapshot_hash
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'ArticleEventCluster verification mismatch'
                );
            END;
            """
        )
        return


def remove_editorial_immutability_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    with schema_editor.connection.cursor() as cursor:
        if vendor == "postgresql":
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_event_verification_append_only "
                "ON editorial_eventclusterverification"
            )
            cursor.execute(
                "DROP FUNCTION IF EXISTS "
                "editorial_guard_event_cluster_verification()"
            )
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_article_event_cluster_guard "
                "ON editorial_articleeventcluster"
            )
            cursor.execute(
                "DROP FUNCTION IF EXISTS "
                "editorial_guard_article_event_cluster()"
            )
            return
        if vendor == "sqlite":
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_event_verification_no_update"
            )
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_event_verification_no_delete"
            )
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_article_event_cluster_no_update"
            )
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_article_event_cluster_no_delete"
            )
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "editorial_article_event_cluster_insert_guard"
            )
            return
    raise RuntimeError(
        "Editorial immutability guard removal does not "
        f"support database vendor {vendor!r}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("collection", "0008_source_access_policy_runtime"),
        ("editorial", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="EventClusterItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("origin_identity_hash", models.CharField(max_length=64)),
                ("independence_group", models.CharField(max_length=120)),
                ("role", models.CharField(max_length=24)),
                ("selection_state", models.CharField(max_length=24)),
                ("decision_reason", models.CharField(max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "cluster",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="members",
                        to="editorial.eventcluster",
                    ),
                ),
                (
                    "run_source_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="event_cluster_memberships",
                        to="collection.runsourceitem",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EventClusterVerification",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("version", models.PositiveIntegerField()),
                ("decision", models.CharField(max_length=32)),
                ("article_type", models.CharField(max_length=40)),
                ("category", models.CharField(max_length=64)),
                ("primary_source_count", models.PositiveIntegerField()),
                ("independent_origin_count", models.PositiveIntegerField()),
                ("decision_reason", models.CharField(max_length=500)),
                ("policy_version", models.CharField(max_length=40)),
                ("policy_hash", models.CharField(max_length=64)),
                ("local_event_date", models.DateField()),
                ("evidence_manifest", models.JSONField(default=list)),
                ("evidence_manifest_hash", models.CharField(max_length=64)),
                ("conflict_manifest", models.JSONField(default=list)),
                ("excluded_source_manifest", models.JSONField(default=list)),
                ("rule_manifest_hash", models.CharField(max_length=64)),
                ("result_manifest_hash", models.CharField(max_length=64)),
                ("verified_at", models.DateTimeField(auto_now_add=True)),
                (
                    "cluster",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="verifications",
                        to="editorial.eventcluster",
                    ),
                ),
                (
                    "origin_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="event_cluster_verifications",
                        to="collection.collectionrun",
                    ),
                ),
                (
                    "supersedes",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="superseded_by",
                        to="editorial.eventclusterverification",
                    ),
                ),
            ],
            options={
                "ordering": ["cluster_id", "version"],
                "base_manager_name": "objects",
            },
        ),
        migrations.AddField(
            model_name="draftarticle",
            name="source_verification",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="articles",
                to="editorial.eventclusterverification",
            ),
        ),
        migrations.AddField(
            model_name="generationattempt",
            name="generation_manifest_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.CreateModel(
            name="ArticleEventCluster",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("role", models.CharField(max_length=24)),
                ("display_order", models.PositiveIntegerField()),
                ("inclusion_reason", models.CharField(max_length=500)),
                ("cluster_snapshot_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "article",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="event_clusters",
                        to="editorial.draftarticle",
                    ),
                ),
                (
                    "event_cluster",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="article_memberships",
                        to="editorial.eventcluster",
                    ),
                ),
                (
                    "verification",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="article_memberships",
                        to="editorial.eventclusterverification",
                    ),
                ),
            ],
            options={
                "ordering": ["article_id", "display_order"],
                "base_manager_name": "objects",
            },
        ),
        migrations.AddConstraint(
            model_name="eventclusteritem",
            constraint=models.UniqueConstraint(
                fields=("cluster", "run_source_item"),
                name="uq_event_cluster_run_item",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventclusterverification",
            constraint=models.UniqueConstraint(
                fields=("cluster", "version"),
                name="uq_event_cluster_verification_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventclusterverification",
            constraint=models.UniqueConstraint(
                fields=("cluster", "origin_run"),
                name="uq_event_cluster_verification_origin_run",
            ),
        ),
        migrations.AddConstraint(
            model_name="eventclusterverification",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(decision="verified_breaking")
                    | (
                        models.Q(
                            category__in=(
                                "regulation_export_control",
                                "factory_supply_disruption",
                                "merger_or_material_earnings",
                                "critical_technology_or_mass_production",
                            )
                        )
                        & (
                            models.Q(primary_source_count__gte=1)
                            | models.Q(independent_origin_count__gte=2)
                        )
                    )
                ),
                name="ck_event_verification_breaking_gate",
            ),
        ),
        migrations.AddConstraint(
            model_name="articleeventcluster",
            constraint=models.UniqueConstraint(
                fields=("article", "event_cluster"),
                name="uq_article_event_cluster",
            ),
        ),
        migrations.AddConstraint(
            model_name="articleeventcluster",
            constraint=models.UniqueConstraint(
                fields=("article", "display_order"),
                name="uq_article_event_display_order",
            ),
        ),
        migrations.RunPython(
            install_editorial_immutability_guards,
            remove_editorial_immutability_guards,
        ),
    ]
