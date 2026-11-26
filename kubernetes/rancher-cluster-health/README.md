# Rancher cluster health: dashboard and image-pull alerts

Install Rancher Monitoring, add a one-screen "Cluster Health" Grafana dashboard that
is empty when the cluster is healthy, and alert when a container cannot pull its image.

## Requirements

- Rancher-managed cluster, `kubectl` with cluster-admin.
- Rancher Monitoring chart `110.0.2+up80.9.1-rancher.22` (tested).

| File | What it is |
|---|---|
| `01-prometheusrule.yaml` | `ImagePullFailingSystem` (critical after 5 min, system namespaces), `ImagePullFailing` (warning after 15 min, everything else) |
| `02-dashboard-configmap.yaml` | The dashboard, auto-loaded by Rancher's Grafana |
| `cluster-health.json` | The same dashboard for a manual Grafana import |
| `03-alertmanagerconfig-{email,teams}.yaml` | Send the alerts to email or a Teams channel |
| `test/` | Two broken workloads and a webhook receiver to prove it end to end |

## Step 1: Install Rancher Monitoring

Skip if it is already installed (check 1.4 first).

1. Rancher UI: open the cluster.
2. **Cluster Tools**, then **Monitoring**, then **Install**.
3. Keep the defaults. On the last page, click **Edit YAML** and add this, so alert
   routes in step 4 see alerts from every namespace:

   ```yaml
   alertmanager:
     alertmanagerSpec:
       alertmanagerConfigMatcherStrategy:
         type: None
   ```

4. Click **Install**, wait for it to finish, then check:

   ```sh
   kubectl -n cattle-monitoring-system get pods     # prometheus, alertmanager, grafana all Running
   kubectl get ns cattle-dashboards                 # exists
   ```

Already installed without the 1.3 setting: **Apps**, **Installed Apps**,
`rancher-monitoring`, **Edit/Upgrade**, **Edit YAML**, add it, **Upgrade**.

## Step 2: Add the alerts and the dashboard

1. Clone this repo and `cd kubernetes/rancher-cluster-health`.
2. Open `01-prometheusrule.yaml`. Edit the system-namespace regex if your core
   services live elsewhere. It appears twice (`=~` and `!~`): keep both identical.
3. Apply:

   ```sh
   kubectl apply -f 01-prometheusrule.yaml
   kubectl apply -f 02-dashboard-configmap.yaml
   ```

4. Check both loaded:

   ```sh
   kubectl -n cattle-monitoring-system get prometheusrule image-pull-failing
   kubectl -n cattle-dashboards get cm cluster-health-dashboard
   ```

## Step 3: Open the dashboard in Grafana

**From Rancher (normal way):**

1. Rancher UI: the cluster, **Monitoring** (left menu).
2. Click the **Grafana** tile. Grafana opens through the Rancher proxy, read-only.
3. **Dashboards**, search `Cluster Health`, open it. It loads within a minute of step 2.

**Without the Rancher UI (port-forward):**

1. `kubectl -n cattle-monitoring-system port-forward deploy/rancher-monitoring-grafana 3000:3000`
2. Open `http://localhost:3000`, **Dashboards**, **Cluster Health**.

**Make it the home page (needs admin):**

1. Get the admin password (user `admin`, chart default `prom-operator`, change it):

   ```sh
   kubectl -n cattle-monitoring-system get secret rancher-monitoring-grafana \
     -o jsonpath='{.data.admin-password}' | base64 -d; echo
   ```

2. Port-forward as above, **Sign in** as `admin`.
3. Open Cluster Health, star it.
4. **Administration**, **Default preferences**, **Home dashboard**: Cluster Health, **Save**.

Healthy, every counter `0` and every table `None: healthy`:

![Cluster Health dashboard, healthy](cluster-health-healthy.png)

With two broken workloads (step 5):

![Cluster Health dashboard with two broken workloads](cluster-health-failing.png)

| Panel | Shows |
|---|---|
| Six counters | containers that cannot start, image pull failures, pods not ready, workloads short of replicas, nodes not ready, firing critical alerts |
| Containers that cannot start | namespace, pod, container, reason (ImagePullBackOff, CrashLoopBackOff, CreateContainerConfigError...) |
| Workloads short of replicas | Deployments, DaemonSets, StatefulSets with unavailable replicas |
| Pods not ready | every pod not Ready, finished Job pods excluded |
| Containers restarting | restarts per container, last hour |
| Firing alerts | everything Alertmanager would send, except Watchdog |

## Step 4: Send the alerts somewhere

1. Pick `03-alertmanagerconfig-email.yaml` or `03-alertmanagerconfig-teams.yaml`.
2. Replace every placeholder (`example.com` addresses, SMTP host or Teams webhook URL).
   Put the real password or webhook URL in the Secret at the top of the file, not in git.
3. `kubectl apply -f 03-alertmanagerconfig-email.yaml` (or `-teams`).
4. Confirm the step 1.3 setting is live. Without it the route only sees alerts about
   pods in `cattle-monitoring-system` and everything else goes to the `null` receiver:

   ```sh
   kubectl -n cattle-monitoring-system get alertmanager \
     -o jsonpath='{.items[0].spec.alertmanagerConfigMatcherStrategy}'   # {"type":"None"}
   ```

## Step 5: Test it (about 7 minutes)

1. Deploy the receiver, its route and two broken workloads:

   ```sh
   kubectl apply -f test/webhook-receiver.yaml
   kubectl apply -f test/alertmanagerconfig-webhook.yaml
   kubectl apply -f test/broken-workloads.yaml
   ```

2. `kubectl get pods -A | grep broken` shows `ErrImagePull` or `ImagePullBackOff`.
3. Dashboard: counters show `2`, both pods in the tables, at once.
4. After 5 minutes `ImagePullFailingSystem` fires. Check the receiver got it:

   ```sh
   kubectl -n alert-test logs deploy/alert-sink
   # ALERT firing ImagePullFailingSystem critical cattle-healthcheck-test broken-core-service-...
   ```

5. After 15 minutes `ImagePullFailing` (warning) fires for the `alert-test` pod.
6. Clean up. The receiver then logs `ALERT resolved`:

   ```sh
   kubectl delete -f test/broken-workloads.yaml
   kubectl -n alert-test logs deploy/alert-sink | tail
   kubectl delete -f test/alertmanagerconfig-webhook.yaml -f test/webhook-receiver.yaml
   ```

## Behaviour

- The stock `KubeContainerWaiting` (1 hour) keeps `reason` as a label. The reason flips
  between `ErrImagePull` and `ImagePullBackOff` on every retry, restarting its timer, so
  it never fires. These rules group without `reason`, so their timer survives the flips.
- `InvalidImageName` is included: it never retries, so it would otherwise only show on
  the dashboard.
- Alertmanager waits `groupWait` (30 seconds) after `for:` before the first message.
- The dashboard needs only kube-state-metrics, which Rancher Monitoring includes.
