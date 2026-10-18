1. Where am I?

kubectl config get-contexts           # clusters you can talk to (* = current)
kubectl config use-context docker-desktop
kubectl get nodes                     # the machines in the cluster (one, here)
kubectl get namespaces                # namespaces separate apps; yours is "litellm"

Tired of typing -n litellm? Make it the default for this context:

kubectl config set-context --current --namespace=litellm

2. What's running?

kubectl -n litellm get pods                  # running containers, their status and restarts
kubectl -n litellm get pods -o wide          # adds pod IP and node
kubectl -n litellm get pods -w               # watch live (Ctrl+C to stop)
kubectl -n litellm get deploy,svc,statefulset,configmap,secret   # several types at once
kubectl -n litellm get all                   # common types in one go

How the pieces fit:
- Pod: one running instance, e.g. pii-service-5fd6487f9b-2pvqw. Pods are disposable, and their names change each time they're replaced.
- Deployment: keeps the right number of pods running and replaces them when they die (litellm, pii-service).
- StatefulSet: like a Deployment, but for things with stable storage (Postgres).
- Service: a stable name and port in front of pods, e.g. pii-service:8000. That's how LiteLLM finds the PII service.
- ConfigMap / Secret: configuration and credentials mounted into pods (your guardrail code, the master key).

3. Why is something broken?

kubectl -n litellm describe pod <pod-name>   # full details; the Events at the bottom say why it won't start
kubectl -n litellm get events --sort-by=.lastTimestamp   # recent cluster events, newest last
kubectl -n litellm logs deploy/litellm       # logs from a Deployment's pod (no pod name needed)
kubectl -n litellm logs deploy/litellm -f    # follow live
kubectl -n litellm logs deploy/litellm --previous   # logs from before the last restart (great for crashes)
kubectl -n litellm logs deploy/pii-service --since=10m

Statuses you'll see:

┌─────────────────────────────────┬─────────────────────────────────────────────────────────────────┐
│             Status              │                             Meaning                             │
├─────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Running                         │ Fine                                                            │
├─────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Pending                         │ Waiting for resources or a volume                               │
├─────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ ContainerCreating               │ Pulling the image or mounting volumes                           │
├─────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ ErrImagePull / ImagePullBackOff │ Can't get the image (as we hit before loading it into the node) │
├─────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ CrashLoopBackOff                │ Keeps crashing; check logs --previous                           │
├─────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Completed                       │ A one-off Job finished (the migrations)                         │
└─────────────────────────────────┴─────────────────────────────────────────────────────────────────┘

4. Get inside

kubectl -n litellm exec deploy/pii-service -- printenv PII_IGNORED_ENTITIES   # run one command
kubectl -n litellm exec -it deploy/litellm -- sh                              # interactive shell (exit to leave)
kubectl -n litellm exec deploy/litellm -- cat /etc/litellm/config.yaml        # see the config LiteLLM loaded

5. Reach it from your machine

kubectl -n litellm port-forward svc/litellm 4000:4000       # localhost:4000 → the litellm Service
kubectl -n litellm port-forward svc/pii-service 8000:8000

A port-forward attaches to one pod. If that pod is replaced, the forward dies and you need to rerun it. That's what happened earlier when I scaled the PII service down.

6. Change what's running

kubectl -n litellm rollout restart deployment/pii-service    # replace pods (e.g. to pick up a rebuilt image)
kubectl -n litellm rollout status deployment/pii-service     # wait until the new pods are ready
kubectl -n litellm rollout history deployment/pii-service
kubectl -n litellm rollout undo deployment/pii-service       # go back to the previous version
kubectl -n litellm scale deployment/pii-service --replicas=2 # run two copies
kubectl -n litellm delete pod <pod-name>                     # kill a pod; its Deployment starts a new one

Try the last one to see Kubernetes self-heal: delete the pii-service pod and watch a new one appear with get pods -w.

7. Read config and secrets

kubectl -n litellm get configmap litellm-guardrail-code -o yaml   # the guardrail code, as deployed
kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d; echo

Secrets are only base64-encoded, not encrypted, which is why base64 -d reveals them.

8. Output tricks

kubectl -n litellm get pod <pod-name> -o yaml                     # everything about an object
kubectl -n litellm get pods -o jsonpath='{.items[*].metadata.name}'   # just the names
kubectl -n litellm get pods -l app.kubernetes.io/name=pii-service     # filter by label
kubectl explain deployment.spec.replicas                              # built-in docs for any field

9. Helm (manages the whole release)

helm list -A                                      # installed releases
helm -n litellm status litellm-guardrail
helm -n litellm get values litellm-guardrail      # values you set (not the chart defaults)
helm -n litellm history litellm-guardrail         # every upgrade (you're on revision 2 after the DATE_TIME change)
helm -n litellm rollback litellm-guardrail 1      # go back to revision 1
helm -n litellm uninstall litellm-guardrail       # remove everything the chart created

As a rule: use Helm to install, upgrade and remove the release, and kubectl to look inside and troubleshoot. If you change things with kubectl (scaling, for example), the next helm upgrade resets them to the chart's values.

kubectl top pods (CPU and memory per pod) needs metrics-server, which Docker Desktop doesn't install by default, so it will probably fail with an error.