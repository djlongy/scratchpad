# Offline docs and Security Labs

Install the Kibana Agent Builder documentation ("Elastic documentation" and "Security
labs") on a disconnected ECK stack, from files on a volume instead of the Elastic CDN.

## Requirements

- ECK with Elasticsearch and Kibana 9.5.x.
- A licence that allows ML (Platinum, Enterprise or trial). The install deploys ELSER,
  which Basic refuses.
- About 2 GiB free native memory on an ML node for the ELSER deployment. A single 4 GiB
  node fails with `insufficient available memory`; 8 GiB works.
- A `ReadWriteMany` volume both Elasticsearch and Kibana can mount, here `airgap-content`.
- About 360 MB of transfer per refresh.

## Usage

### 1. Connected side: download

```sh
mkdir -p kb models
U=https://kibana-knowledge-base-artifacts.elastic.co
curl -fsS "$U/" -o kb/index.xml
for p in elasticsearch kibana observability openapi security; do
  curl -fsS -o "kb/kb-product-doc-$p-9.5.zip" "$U/kb-product-doc-$p-9.5.zip"
done
SL=$(grep -oE 'security-labs-[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.zip' kb/index.xml | sort -u | tail -1)
curl -fsS -o "kb/$SL" "$U/$SL"
for e in metadata.json vocab.json pt; do
  curl -fsS -o "models/elser_model_2_linux-x86_64.$e" \
    "https://ml-models.elastic.co/elser_model_2_linux-x86_64.$e"
done
sha256sum kb/* models/* > SHA256SUMS
```

Change `9.5` to your Kibana minor version.

### 2. Move `kb/`, `models/` and `SHA256SUMS` across the gap

Use your normal transfer path. On arrival, run `sha256sum -c SHA256SUMS`.

### 3. Disconnected side: put the files on the volume

Use any pod that mounts the volume at `/data`:

```sh
kubectl -n elastic cp kb     <helper-pod>:/data/kb
kubectl -n elastic cp models <helper-pod>:/data/models
```

### 4. Point Elasticsearch at the model files

Merge this into the Elasticsearch resource:

```yaml
spec:
  nodeSets:
  - name: default
    config:
      xpack.ml.model_repository: file:///usr/share/elasticsearch/config/models
    podTemplate:
      spec:
        containers:
        - name: elasticsearch
          volumeMounts:
          - {name: airgap, mountPath: /usr/share/elasticsearch/config/models, subPath: models, readOnly: true}
        volumes:
        - name: airgap
          persistentVolumeClaim: {claimName: airgap-content, readOnly: true}
```

The path must be under the Elasticsearch config directory.

### 5. Point Kibana at the docs files

Merge this into the Kibana resource:

```yaml
spec:
  config:
    xpack.productDocBase.artifactRepositoryUrl: file:///usr/share/kibana/airgap/kb
  podTemplate:
    spec:
      containers:
      - name: kibana
        volumeMounts:
        - {name: airgap, mountPath: /usr/share/kibana/airgap/kb, subPath: kb, readOnly: true}
      volumes:
      - name: airgap
        persistentVolumeClaim: {claimName: airgap-content, readOnly: true}
```

`file://` needs no CA trust and no web server.

### 6. Apply and install

```sh
kubectl apply -f elasticsearch.yaml -f kibana.yaml   # ECK restarts the pods once
```

In Kibana, open **Agent Builder > Documentation** and install **Elastic documentation**
and **Security labs**. Each install takes several minutes. The same from the command line:

```sh
H=(-H 'kbn-xsrf: x' -H 'x-elastic-internal-origin: kibana' -H 'elastic-api-version: 1' -H 'Content-Type: application/json')
curl -sk -u "elastic:$PW" "${H[@]}" -X POST "https://<kibana>/internal/product_doc_base/install" -d '{}'
curl -sk -u "elastic:$PW" "${H[@]}" -X POST "https://<kibana>/internal/product_doc_base/install" -d '{"resourceType":"security_labs"}'
```

## Behaviour

- Kibana reads `index.xml`, then downloads only the keys it needs: product docs for its
  own minor version, and the newest `security-labs-YYYY.MM.DD.zip`. A file listed in
  `index.xml` but missing from the volume is only a problem if Kibana selects it.
- Use the zips without a `--` suffix. Those are the ELSER variants. Files named
  `--.multilingual-e5-small...` or `--.jina-embeddings...` are for other inference endpoints.
- To refresh, copy the new files over the old ones and reinstall from the same page.
  No restart is needed.

## Expected result

Both rows on the Documentation page show as installed. Check:

```sh
curl -sk -u "elastic:$PW" "${H[@]}" "https://<kibana>/internal/product_doc_base/status"
curl -sk -u "elastic:$PW" "${H[@]}" "https://<kibana>/internal/product_doc_base/status?resourceType=security_labs"
```

Expect `"overall":"installed"` and `"status":"installed"`.

## Troubleshooting

`Could not start deployment because no suitable nodes were found ... insufficient
available memory`: the ML node is too small for ELSER. Raise the Elasticsearch memory, stop
the failed deployment with
`POST _ml/trained_models/.elser-2-elasticsearch/deployment/_stop?force=true`, then
install again.

A single-node cluster that never restarts after a resource change is yellow because its
replica shards have nowhere to go, and ECK does not restart a node while the cluster is
yellow. Set `number_of_replicas: 0` on the indices, or add a second node.
