# NiFi 2.x: MergeContent stops after node restarts, and only a component start clears it

A clustered NiFi 2.12 flow stopped at MergeContent (Defragment) after two nodes restarted. A
rolling restart of every node did not clear it. Stopping and starting every process group and
processor did. This note explains why, shows a repro on a 3-node 2.12.0 cluster, and lists the
flow changes that stop it happening again.

Flow shape: a Kafka consumer, Notify, delete a tracking record, hash the content, DetectDuplicate,
DecryptContentPGP, UnpackContent, MergeContent (Defragment). MergeContent forks to a failure
group (UpdateAttribute, PackageFlowFile, PutS3Object, log) and to the success path.

## Cause

A connection out of MergeContent reached its back pressure threshold because the component it
feeds was not running. NiFi does not schedule a processor while any of its outgoing connections
is full, so MergeContent stopped, and everything upstream backed up behind it.

1. Node restarts leave incomplete Defragment bins:
   - A partition-by-attribute connection rehashes when the cluster topology changes. Fragments
     of one bundle queued before and after the change can land on different nodes.
   - A redelivered Kafka record or a replayed session can bring a second copy of a
     `fragment.index`. MergeContent keeps the first copy in the bin and does not accept the
     second.
2. With `Max Bin Age` unset, an incomplete bin leaves only when `Maximum Number of Bins` is
   exceeded (default 5). It then goes to `failure` with this message:
   `Cannot Defragment FlowFiles with Fragment Identifier ... because the expected number of
   fragments is N but found only M fragments`.
3. `failure` feeds something that is stopped or disabled: a holding processor, a "buffer", or
   an input port of a stopped group. Its queue fills. Back pressure applies per node, so
   MergeContent stops on every node where that queue is full.
4. A restart restores the queued FlowFiles and every component's run state. The stopped
   component is still stopped and the full queue is still full. Starting every component starts
   the stopped one, its queue drains, and MergeContent is scheduled again.

Of the causes checked, this is the only one that fits all three facts: the trigger is a node
restart, a rolling restart does not recover it, and a component start does.

## Repro (3 nodes, NiFi 2.12.0)

One unversioned process group:

```text
GenerateFlowFile (100 KB, every 10 s) -> SegmentContent (5 KB) --segments--> MergeContent
GenerateFlowFile (1 KB, every 2 s, attributes:                  --success--->  (Defragment,
   fragment.identifier=${UUID()}, fragment.index=0,                             Max Bin Age unset,
   fragment.count=2)                                                            5 bins)
MergeContent --merged--> UpdateAttribute (running, auto-terminate)
MergeContent --failure--> UpdateAttribute "buffer" (STOPPED), back pressure 40 objects
```

The second generator stands in for fragments whose siblings are on another node: every one of
them is an orphan.

| Step | Result |
|---|---|
| Run for 3 minutes | failure queue 120 (40 on each node), MergeContent no longer scheduled, segment queue grows, merged output stops |
| Rolling restart of all 3 nodes | still stalled: failure queue 120, segment queue 2620 |
| Start every processor in the group | failure queue drains, segment queue 0 within 45 s |
| Fix: failure connection 0 objects / 0 B, 3 day expiration, buffer left stopped | failure queue passed 200, segment queue stayed 0 |

The repro feeds orphans directly. It does not prove that a restart splits fragments.

## Related Apache NiFi Jira issues

Checked on 2026-10-30. No issue on file matches this case directly. 2.12.0 is the latest
release.

| Issue | Status | Relevance |
|---|---|---|
| [NIFI-16432](https://issues.apache.org/jira/browse/NIFI-16432) KubernetesLeaderElectionManager errors may stop the scheduling of primary only, CRON driven processors | Patch Available, no fix version | Also recovered by stop/start. Applies only if a primary-node CRON processor feeds the merge path |
| [NIFI-16088](https://issues.apache.org/jira/browse/NIFI-16088) Add logging related to primary node only cron scheduling | Fixed in 2.11.0 | The logging for NIFI-16432 is in 2.12 |
| [NIFI-16084](https://issues.apache.org/jira/browse/NIFI-16084) Load-balanced connection desynchronizes its socket after an in-flight transaction is unregistered | Fixed in 2.11.0 | A stuck partition-by-attribute queue after a restart. Seeing it on 2.12 would be a regression |
| [NIFI-16446](https://issues.apache.org/jira/browse/NIFI-16446) Swap in FlowFiles when active queue has room instead of when active queue empty | Open | A fragment swapped out of a queue holding more than 20000 FlowFiles can starve a Defragment bin |
| [NIFI-14717](https://issues.apache.org/jira/browse/NIFI-14717) MergeContent should release a bin when maxBinAge condition is fulfilled | Fixed in 2.5.0 | `Max Bin Age` works on 2.12 |

## How to confirm it on your cluster

1. **Flow Configuration History** (global menu) for the recovery window. Every component the
   start actually changed is listed as a Start action. A failure, buffer or holding component in
   that list was stopped before the recovery. That confirms the cause.
2. `conf/flow.json.gz` from before the recovery: `scheduledState` of every component on
   MergeContent's `failure` path.
3. `nifi-app.log` on every node from the first restart onwards:
   - `Cannot Defragment FlowFiles with Fragment Identifier`
   - `Expected to receive Transaction Completion Indicator` and
     `Failed to receive FlowFiles for Load Balancing` (NIFI-16084)
   - `KubernetesLeaderElectionManager` exceptions and `CronSchedulingAgent` warnings
     (NIFI-16432)
   - consumer group rebalance lines from the Kafka consumer
4. Status history of MergeContent's outgoing connections: queued count against the back pressure
   threshold, per node.

Kubernetes readiness is not cluster membership. In the repro, one restarted node was ready for
about 7 minutes before it reconnected (`Lack of Heartbeat`), and requests replicated to it
failed with HTTP 409 until the log showed `Node reconnected`.

## Fix

1. Set `Max Bin Age` on MergeContent to longer than the slowest normal bundle, and set
   `Maximum Number of Bins` higher than the number of bundles one node holds at once.
2. Keep the failure path running end to end. If a component on it is stopped on purpose (to
   hold data for inspection), the connection into it must not push back: set 0 objects, 0 B and
   a FlowFile expiration. A full queue there then ages out instead of stopping MergeContent.
   Only drop data this way where there is a resend or replay path.
3. Do not load balance between UnpackContent and MergeContent. UnpackContent creates all the
   fragments of one bundle on one node. Load balance the whole bundle before it.
4. Put Notify after DetectDuplicate, so a redelivered record is not counted twice.
5. Alert on back pressure on any connection out of MergeContent or Wait, and on MergeContent
   ERROR bulletins.
6. Recovery: find the full outgoing queue and start the component it feeds. Restarting nodes
   does not help.
