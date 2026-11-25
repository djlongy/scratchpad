# ECK package registry

Deploy the offline EPR with ECK; connect Kibana through `packageRegistryRef`.

## Requirements

ECK 3.5 with the PackageRegistry CRD, Elastic 9.5.2 and an enforcing NetworkPolicy CNI.

## Usage

1. Create namespace `elastic` and its `registry-pull` Secret using your internal registry credentials.
2. Import the full EPR production image into that registry. Replace the image digest in `package-registry.yaml`; stage it on eligible nodes with at least 80 GiB of image disk.
3. Set the namespace and resource names in all three manifests to match your stack. The policy permits only the named Kibana's pods in the same namespace. Ensure no other policy grants EPR egress.
4. Apply the registry and policy:

   ```sh
   kubectl apply -f package-registry-networkpolicy.yaml -f package-registry.yaml
   kubectl -n elastic rollout status deployment/registry-epr --timeout=60m
   ```

5. Merge `kibana-package-registry.yaml` into your existing Kibana resource and apply it. Remove its explicit `xpack.fleet.registryUrl`; ECK supplies the URL and CA trust. Keep the file-mirror and OIDC settings from `kibana.yaml`.

## Expected result

ECK creates the Deployment, HTTPS Service and certificates. Verify `kubectl -n elastic get kibana kb -o jsonpath='{.status.packageRegistryAssociationStatus}'` returns `Established`. The startup probe allows 30 minutes for indexing; pre-stage the image to avoid a cold pull exceeding the generated Deployment's ten-minute progress deadline.

## Out of scope

This replaces the chart's EPR workload. Do not install both EPR implementations. The binary, Defend, GeoIP, model and documentation file server remains separate; EPR does not serve those files.
