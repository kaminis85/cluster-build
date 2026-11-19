# cluster-build

Lab playbook for a 2-node ONTAP cluster. Values are in `vars/lab.yml`.

The console step runs first on both nodes, because a factory node has no management IP yet. The admin password in `vars/lab.yml` is set on both consoles. Cluster create is a REST post to the first node that includes that password, both nodes, and the cluster-mgmt address. Node 2 joins in that same request. DNS, the default route, location, and the cluster-mgmt port are applied after the create job succeeds.

## Run

Edit `vars/lab.yml` and replace both `REPLACE_ME` console commands. Then:

```bash
pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/build_cluster.yml
```

Set `bootstrap_console: false` to skip the consoles on a later run, after both node-mgmt IPs exist.

Check the console driver without hardware:

```bash
python3 scripts/test_console_flow.py
```
