import copy
import ipaddress
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/archers"
HELM = os.environ.get("HELM_BIN", "helm")
KUBECONFORM = os.environ.get("KUBECONFORM_BIN", "kubeconform")
BASE = {
    "images": {
        "backend": "registry.example/backend@sha256:" + "a" * 64,
        "web": "registry.example/web@sha256:" + "b" * 64,
    },
    "existingSecret": "runtime",
    "storage": {"artifactsClaim": "artifacts", "modelClaim": "model"},
    "application": {
        "origin": "https://archers.internal.example",
        "qdrantUrl": "http://qdrant.internal.example:6333",
    },
    "networkPolicy": {
        "databaseCidrs": ["10.0.1.0/24"],
        "qdrantCidrs": ["10.0.2.10/32"],
    },
}


def render(override=None, *, example=None, only=None, release="archers"):
    with tempfile.TemporaryDirectory() as directory:
        values = Path(directory) / "values.yaml"
        values.write_text(yaml.safe_dump(override or {}))
        base = Path(directory) / "base.yaml"
        base.write_text(yaml.safe_dump(BASE))
        command = [
            HELM,
            "template",
            release,
            str(CHART),
            "--namespace",
            "archers",
            "-f",
            str(base),
        ]
        if example:
            command.extend(["-f", str(example)])
        command.extend(["-f", str(values)])
        if only:
            command.extend(["--show-only", only])
        return subprocess.run(command, capture_output=True, text=True, check=False)


def documents(result):
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


class HelmPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which(HELM) or not shutil.which(KUBECONFORM):
            raise RuntimeError(
                "Install Helm and kubeconform, or set HELM_BIN and KUBECONFORM_BIN."
            )

    def test_defaults_are_private_single_replicas_and_existing_storage_only(self):
        manifests = documents(render())
        self.assertFalse(
            any(
                m["kind"]
                in {
                    "Secret",
                    "PersistentVolumeClaim",
                    "Ingress",
                    "Job",
                    "HorizontalPodAutoscaler",
                }
                for m in manifests
            )
        )
        deployments = [m for m in manifests if m["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 3)
        for deployment in deployments:
            self.assertEqual(deployment["spec"]["replicas"], 1)
            self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
            spec = deployment["spec"]["template"]["spec"]
            container = spec["containers"][0]
            component = container["name"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertTrue(spec["securityContext"]["runAsNonRoot"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(
                container["securityContext"]["capabilities"]["drop"], ["ALL"]
            )
            self.assertNotIn("hostPath", yaml.safe_dump(spec))
            mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
            self.assertNotIn("source", mounts)
            if component == "worker":
                self.assertTrue(mounts["model"]["readOnly"])
                self.assertEqual(container["command"], ["archers", "worker"])
            elif component == "api":
                self.assertNotIn("model", mounts)
                self.assertEqual(
                    container["readinessProbe"]["httpGet"]["path"],
                    "/api/v1/health/live",
                )
            else:
                self.assertNotIn("secrets", mounts)
                self.assertNotIn("artifacts", mounts)
        for service in (m for m in manifests if m["kind"] == "Service"):
            self.assertEqual(service["spec"]["type"], "ClusterIP")

    def test_chart_settings_validate_against_real_application(self):
        from archers.config import Settings

        config = next(m for m in documents(render()) if m["kind"] == "ConfigMap")
        settings = yaml.safe_load(config["data"]["settings.yaml"])
        settings.update(
            database_url="postgresql+psycopg://test@localhost/test",
            signing_key="s" * 32,
            operator_token="o" * 32,
        )
        validated = Settings.model_validate(settings)
        self.assertTrue(validated.secure_cookie)
        self.assertEqual(validated.network_mode, "airgap")
        canonical = (ROOT / "web/nginx.conf").read_text()
        expected = canonical.replace(
            "http://api:8000", "http://archers-archers-api:8000"
        )
        self.assertEqual(config["data"]["nginx.conf"].strip(), expected.strip())

    def test_source_has_readonly_mounts_and_independent_health(self):
        override = {
            "source": {
                "enabled": True,
                "existingClaim": "inbox",
                "healthClaim": "health",
            }
        }
        manifests = documents(render(override))
        watcher = next(
            m
            for m in manifests
            if m["kind"] == "Deployment" and m["metadata"]["name"].endswith("-watcher")
        )
        spec = watcher["spec"]["template"]["spec"]
        mounts = {v["name"]: v for v in spec["containers"][0]["volumeMounts"]}
        for name in ("source", "source-health"):
            self.assertTrue(mounts[name]["readOnly"])
            self.assertNotIn("subPath", mounts[name])
        config = next(m for m in manifests if m["kind"] == "ConfigMap")
        settings = yaml.safe_load(config["data"]["settings.yaml"])
        self.assertEqual(settings["network_mounts"], ["inbox"])
        self.assertEqual(
            settings["mount_health_files"], {"inbox": "/run/source-health/inbox.json"}
        )

    def test_scaling_targets_and_limits(self):
        overlay = yaml.safe_load(
            (ROOT / "deploy/helm/examples/scaling-values.yaml").read_text()
        )
        manifests = documents(render(overlay))
        hpas = [m for m in manifests if m["kind"] == "HorizontalPodAutoscaler"]
        self.assertEqual(len(hpas), 3)
        self.assertEqual(
            len([m for m in manifests if m["kind"] == "PodDisruptionBudget"]), 3
        )
        for hpa in hpas:
            self.assertEqual(hpa["spec"]["maxReplicas"], 2)
            self.assertEqual(
                hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"], 300
            )
            target = hpa["spec"]["scaleTargetRef"]["name"]
            deployment = next(
                m
                for m in manifests
                if m["kind"] == "Deployment" and m["metadata"]["name"] == target
            )
            self.assertNotIn("replicas", deployment["spec"])

    def test_unsafe_or_inconsistent_values_are_rejected(self):
        cases = [
            {"images": {"backend": "registry.example/backend:latest"}},
            {"application": {"origin": "http://archers.internal.example"}},
            {"application": {"qdrantUrl": "http://::::"}},
            {"networkPolicy": {"qdrantPort": 443}},
            {"application": {"maxUploadBytes": 26214401}},
            {"worker": {"replicas": 0}},
            {"worker": {"resources": {"requests": {"cpu": "0"}}}},
            {"worker": {"autoscaling": {"minReplicas": 3, "maxReplicas": 2}}},
            {"worker": {"replicas": 2}},
            {"networkPolicy": {"enabled": False}},
            {"networkPolicy": {"dnsPodLabels": None}},
            {"networkPolicy": {"databaseCidrs": ["0.0.0.0/0"]}},
            {"networkPolicy": {"databaseCidrs": ["10.0.0.0/0"]}},
            {"networkPolicy": {"databaseCidrs": ["10.999.0.0/16"]}},
            {"networkPolicy": {"databaseCidrs": ["169.254.169.254/32"]}},
            {"source": {"enabled": True, "existingClaim": "inbox"}},
            {"ingress": {"enabled": True}},
            {"unrecognizedSetting": True},
            {"scaling": {"acknowledgeUncertified": True}, "worker": {"replicas": 2}},
            {"storage": {"sharedArtifactsConfirmed": True}, "worker": {"replicas": 2}},
            {
                "storage": {"sharedArtifactsConfirmed": True},
                "scaling": {
                    "acknowledgeUncertified": True,
                    "databaseConnectionBudget": 45,
                },
                "worker": {"replicas": 2},
            },
        ]
        scaled = yaml.safe_load(
            (ROOT / "deploy/helm/examples/scaling-values.yaml").read_text()
        )
        over_limit = copy.deepcopy(scaled)
        over_limit["worker"]["autoscaling"]["maxReplicas"] = 3
        cases.append(over_limit)
        for case in cases:
            with self.subTest(case=case):
                result = render(case)
                self.assertNotEqual(result.returncode, 0, repr(case))
                self.assertIn("Error", result.stderr)

    def test_network_boundaries(self):
        policies = [m for m in documents(render()) if m["kind"] == "NetworkPolicy"]
        default = next(
            m for m in policies if m["metadata"]["name"].endswith("-default-deny")
        )
        self.assertEqual(default["spec"]["ingress"], [])
        self.assertEqual(default["spec"]["egress"], [])
        for policy in policies:
            self.assertEqual(
                policy["spec"]["podSelector"]["matchLabels"][
                    "app.kubernetes.io/instance"
                ],
                "archers",
            )
            for rule in policy["spec"].get("egress", []):
                for destination in rule.get("to", []):
                    if "ipBlock" in destination:
                        self.assertTrue(
                            ipaddress.ip_network(
                                destination["ipBlock"]["cidr"]
                            ).is_private
                        )
        web = next(m for m in policies if m["metadata"]["name"].endswith("-web-to-api"))
        destination = web["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"]
        self.assertEqual(destination["app.kubernetes.io/component"], "api")
        self.assertEqual(
            web["spec"]["egress"][0]["ports"], [{"protocol": "TCP", "port": 8000}]
        )

    def test_migration_is_standalone_and_has_no_data_mounts(self):
        manifests = documents(
            render({"migration": {"enabled": True}}, only="templates/migration.yaml")
        )
        self.assertCountEqual(
            [m["kind"] for m in manifests], ["ConfigMap", "NetworkPolicy", "Job"]
        )
        job = next(m for m in manifests if m["kind"] == "Job")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 600)
        spec = job["spec"]["template"]["spec"]
        self.assertEqual(spec["containers"][0]["command"], ["archers", "migrate"])
        self.assertFalse(any("persistentVolumeClaim" in v for v in spec["volumes"]))
        config = next(m for m in manifests if m["kind"] == "ConfigMap")
        self.assertEqual(
            spec["volumes"][0]["configMap"]["name"], config["metadata"]["name"]
        )
        long_names = documents(
            render({"migration": {"enabled": True}}, release="a" * 40)
        )
        for manifest in long_names:
            limit = 63 if manifest["kind"] in {"Job", "Service"} else 253
            self.assertLessEqual(len(manifest["metadata"]["name"]), limit)

    def test_cloud_examples_require_private_consent_and_match_kubernetes_schemas(self):
        overlay = yaml.safe_load(
            (ROOT / "deploy/helm/examples/scaling-values.yaml").read_text()
        )
        for cloud in ("aws", "azure"):
            example = ROOT / f"deploy/helm/examples/{cloud}-values.yaml"
            self.assertNotEqual(render(example=example).returncode, 0)
            for scaled in (False, True):
                values = copy.deepcopy(overlay) if scaled else {}
                values["ingress"] = {"privateAccessConfirmed": True}
                values["migration"] = {"enabled": True}
                result = render(values, example=example)
                manifests = documents(result)
                ingress = next(m for m in manifests if m["kind"] == "Ingress")
                self.assertEqual(
                    ingress["spec"]["tls"][0]["hosts"], ["archers.internal.example"]
                )
                validation = subprocess.run(
                    [
                        KUBECONFORM,
                        "-strict",
                        "-summary",
                        "-kubernetes-version",
                        "1.33.0",
                    ],
                    input=result.stdout,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    validation.returncode, 0, validation.stdout + validation.stderr
                )


if __name__ == "__main__":
    unittest.main()
