#!/usr/bin/env python3

import sys
import os
import argparse # Added for command-line arguments
from collections import defaultdict
from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError as RuamelYAMLError


# Define the mapping from Kubernetes Kind to output filename
# This can be customized as needed.
KIND_TO_FILENAME_MAP = {
    "ConfigMap": "configmaps.yaml",
    "Deployment": "deployments.yaml",
    "HorizontalPodAutoscaler": "hpa.yaml",
    "Namespace": "namespaces.yaml",
    "ServiceAccount": "service-accounts.yaml",
    "Service": "services.yaml",
    "Telemetry": "telemetry.yaml", # Istio specific
    # RBAC Components
    "Role": "rbac.yaml",
    "ClusterRole": "rbac.yaml",
    "RoleBinding": "rbac.yaml",
    "ClusterRoleBinding": "rbac.yaml",
    # Webhook Configurations
    "MutatingWebhookConfiguration": "webhooks.yaml",
    "ValidatingWebhookConfiguration": "webhooks.yaml",
    # Istio "Policy-like" CRDs and Networking
    "AuthorizationPolicy": "policies.yaml",
    "PeerAuthentication": "policies.yaml",
    "RequestAuthentication": "policies.yaml",
    "Sidecar": "policies.yaml",
    "EnvoyFilter": "policies.yaml",
    "WasmPlugin": "policies.yaml",
    "Gateway": "policies.yaml", # Istio Gateway
    "VirtualService": "policies.yaml",
    "DestinationRule": "policies.yaml",
    "ServiceEntry": "policies.yaml",
    "WorkloadEntry": "policies.yaml",
    "WorkloadGroup": "policies.yaml",
    "PodDisruptionBudget": "policies.yaml",
    "Telemetry": "telemetry.yaml",
    "IstioOperator": "istiooperators.yaml", # Often part of istioctl output
    "CustomResourceDefinition": "crds.yaml", # For CRDs themselves
    # Add more kinds as needed
}

# Files requested by the user (for kustomization.yaml)
REQUESTED_FILES_FOR_KUSTOMIZATION = [
    "configmaps.yaml",
    "deployments.yaml",
    "hpa.yaml",
    "namespaces.yaml",
    "policies.yaml",
    "rbac.yaml",
    "service-accounts.yaml",
    "services.yaml",
    "telemetry.yaml",
    "webhooks.yaml",
    # Potentially useful additions if they appear
    #"crds.yaml",
    #"istiooperators.yaml",
    "others.yaml" # Catch-all for unmapped kinds
]

def main():
    parser = argparse.ArgumentParser(
        description="Split Istio manifests from stdin into categorized files in an output directory."
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="istio_manifests_output", # Default output directory name
        help="The directory where YAML files will be saved (default: istio_manifests_output)"
    )
    args = parser.parse_args()
    output_dir = args.output_dir

    # Create the output directory if it doesn't exist
    try:
        os.makedirs(output_dir, exist_ok=True)
        print(f"Output directory: {os.path.abspath(output_dir)}")
    except OSError as e:
        print(f"Error: Could not create output directory '{output_dir}': {e}", file=sys.stderr)
        sys.exit(1)

    # Initialize ruamel.yaml instance for round-trip preservation
    yaml = YAML()
    yaml.preserve_quotes = True
    # yaml.indent(mapping=2, sequence=4, offset=2) # Optional: to enforce specific indent

    # Dictionary to store YAML content for each file
    output_files_content = defaultdict(list)
    # Set to keep track of which files actually get content (just filenames)
    created_filenames = set()

    try:
        yaml_documents = list(yaml.load_all(sys.stdin))
    except RuamelYAMLError as e:
        print(f"Error parsing YAML input: {e}", file=sys.stderr)
        if hasattr(e, 'problem_mark') and e.problem_mark:
            print(f"Error found near line {e.problem_mark.line + 1}, column {e.problem_mark.column + 1}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred while reading/parsing stdin: {e}", file=sys.stderr)
        sys.exit(1)

    if not yaml_documents:
        print("No YAML input received from stdin.", file=sys.stderr)
        return

    for doc in yaml_documents:
        if doc is None:
            continue
        kind = doc.get("kind")
        filename = KIND_TO_FILENAME_MAP.get(kind, "others.yaml") # Just the filename, not path
        output_files_content[filename].append(doc)
        created_filenames.add(filename)

    # Write the collected YAML documents to their respective files in the output directory
    for filename, docs in output_files_content.items():
        if not docs:
            continue
        
        output_filepath = os.path.join(output_dir, filename)
        try:
            with open(output_filepath, "w") as f:
                yaml.dump_all(docs, f)
            print(f"Written {len(docs)} resource(s) to {output_filepath}")
        except IOError as e:
            print(f"Error writing to file {output_filepath}: {e}", file=sys.stderr)
        except RuamelYAMLError as e:
            print(f"Error serializing YAML for {output_filepath}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"An unexpected error occurred while writing {output_filepath}: {e}", file=sys.stderr)

    # Generate kustomization.yaml in the output directory
    kustomization_yaml = YAML()
    kustomization_yaml.indent(mapping=2, sequence=2, offset=0) # Common kustomize style

    kustomization_content = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}
    
    # Resources in kustomization.yaml are relative to kustomization.yaml itself
    kustomization_resources = sorted([
        fname for fname in created_filenames 
        if fname != "kustomization.yaml" and fname in REQUESTED_FILES_FOR_KUSTOMIZATION
    ])

    if not kustomization_resources:
        print(f"No resources found to include in kustomization.yaml within {output_dir}.", file=sys.stderr)
    else:
        kustomization_content["resources"] = kustomization_resources
        kustomization_filepath = os.path.join(output_dir, "kustomization.yaml")
        try:
            with open(kustomization_filepath, "w") as f:
                kustomization_yaml.dump(kustomization_content, f)
            print(f"Written {kustomization_filepath}")
        except IOError as e:
            print(f"Error writing to {kustomization_filepath}: {e}", file=sys.stderr)
        except RuamelYAMLError as e:
            print(f"Error serializing YAML for {kustomization_filepath}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"An unexpected error occurred while writing {kustomization_filepath}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()