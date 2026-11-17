mock_provider "helm" {}

variables {
  namespace                  = "archers"
  migration_revision_applied = "0003_automatic_ingestion"
  values_yaml                = "existingSecret: archers-runtime\n"
}

run "private_existing_namespace_release" {
  command = plan

  assert {
    condition     = helm_release.archers.namespace == "archers" && !helm_release.archers.create_namespace
    error_message = "Deployment must use the operator's existing namespace."
  }
  assert {
    condition     = helm_release.archers.wait && helm_release.archers.atomic && helm_release.archers.timeout == 900
    error_message = "Application rollout must wait and fail explicitly."
  }
  assert {
    condition     = strcontains(helm_release.archers.description, "schema 0003_automatic_ingestion")
    error_message = "Migration acknowledgement must be tied to the rollout."
  }
}

run "reject_missing_migration_gate" {
  command = plan
  variables {
    migration_revision_applied = ""
  }
  expect_failures = [var.migration_revision_applied]
}

run "reject_automatic_migration_during_rollout" {
  command = plan
  variables {
    values_yaml = "migration:\n  enabled: true\n"
  }
  expect_failures = [var.values_yaml]
}

run "reject_invalid_values" {
  command = plan
  variables {
    values_yaml = "invalid: [yaml"
  }
  expect_failures = [var.values_yaml]
}
