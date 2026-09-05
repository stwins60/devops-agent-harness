"""Shell command classification: SAFE / CAUTION / DANGEROUS / FORBIDDEN.

Classification is regex based and evaluated on the *normalised* command
string (collapsed whitespace, lower-cased program name). Patterns can be
extended from ``policies/commands.yaml`` but built-in DANGEROUS / FORBIDDEN
patterns can never be removed or downgraded by configuration.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, Optional

from agent.models import CommandClass, PermissionLevel


@dataclass
class CommandRule:
    pattern: str
    klass: CommandClass
    permission: PermissionLevel
    reason: str = ""
    builtin: bool = False
    regex: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.regex = re.compile(self.pattern, re.IGNORECASE)


# Order matters: first match wins, so FORBIDDEN and DANGEROUS come first.
_BUILTIN_RULES: list[CommandRule] = [
    # ---- FORBIDDEN: never allowed, regardless of approval ----------------
    CommandRule(r"^rm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+(/|/\*|~|\$HOME|\*)\s*$", CommandClass.FORBIDDEN, PermissionLevel.DESTROY, "recursive delete of root/home", True),
    CommandRule(r"^(mkfs|dd\s+.*of=/dev/|shred\s+.*/dev/)", CommandClass.FORBIDDEN, PermissionLevel.DESTROY, "filesystem destruction", True),
    CommandRule(r":\(\)\s*\{\s*:\|:&\s*\};:", CommandClass.FORBIDDEN, PermissionLevel.DESTROY, "fork bomb", True),
    CommandRule(r"^(curl|wget)\b.*\|\s*(sudo\s+)?(ba)?sh\b", CommandClass.FORBIDDEN, PermissionLevel.DESTROY, "pipe remote script to shell", True),
    CommandRule(r"(cat|echo|printenv|env)\b.*(\.aws/credentials|id_rsa|\.kube/config|/etc/shadow)", CommandClass.FORBIDDEN, PermissionLevel.READ, "credential exfiltration", True),
    CommandRule(r"^(printenv|env)\s*$", CommandClass.FORBIDDEN, PermissionLevel.READ, "dumping full environment may expose secrets", True),
    CommandRule(r"^kubectl\s+get\s+secrets?\b.*(-o\s*(yaml|json)|--output)", CommandClass.FORBIDDEN, PermissionLevel.READ, "dumping secret values", True),
    CommandRule(r"^aws\s+iam\s+(attach-user-policy|put-user-policy|create-access-key|add-user-to-group)\b.*(Administrator|\*)", CommandClass.FORBIDDEN, PermissionLevel.DESTROY, "IAM privilege escalation", True),
    # ---- DANGEROUS: explicit approval always ------------------------------
    CommandRule(r"^rm\s+-[a-z]*r", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "recursive delete", True),
    CommandRule(r"^terraform\s+destroy\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "terraform destroy", True),
    CommandRule(r"^terraform\s+state\s+(rm|mv|push)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "terraform state mutation", True),
    CommandRule(r"^kubectl\s+delete\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "kubectl delete", True),
    CommandRule(r"^kubectl\s+drain\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "node drain", True),
    CommandRule(r"^helm\s+(uninstall|delete)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "helm uninstall", True),
    CommandRule(r"^aws\s+\S+\s+(delete|terminate|deregister|remove|purge|disable)[a-z\-]*\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "aws delete*", True),
    CommandRule(r"^aws\s+iam\s+(attach|put|create|add|detach|delete|update)[a-z\-]*\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "IAM mutation", True),
    CommandRule(r"^aws\s+s3\s+(rm|rb)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "s3 removal", True),
    CommandRule(r"\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "destructive SQL", True),
    CommandRule(r"^(psql|mysql|mongosh?)\b.*(migrate|migration)", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "database migration", True),
    CommandRule(r"^(alembic\s+(upgrade|downgrade)|flyway\s+migrate|liquibase\s+update|rails\s+db:migrate|prisma\s+migrate\s+deploy)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "database migration", True),
    CommandRule(r"^git\s+push\b.*(--force|-f\b|\+)", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "force push", True),
    CommandRule(r"^git\s+(reset\s+--hard|clean\s+-[a-z]*f|branch\s+-D)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "destructive git", True),
    CommandRule(r"^(iptables|nft|ufw)\b.*(-F|flush|disable|--policy\s+ACCEPT)", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "firewall policy removal", True),
    CommandRule(r"^(userdel|groupdel|passwd|chpasswd)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "user/credential mutation", True),
    CommandRule(r"^(chmod|chown)\s+(-R|--recursive)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "recursive permission change", True),
    CommandRule(r"^(shutdown|reboot|halt|poweroff|init\s+[06])\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "host power state", True),
    CommandRule(r"^docker\s+(system\s+prune|volume\s+(rm|prune)|rm\s+-f|rmi)\b", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "docker destructive", True),
    CommandRule(r"^kubectl\s+(apply|create|patch|edit|replace)\b.*(clusterrole|clusterrolebinding|rolebinding|psp|networkpolic)", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "security policy change", True),
    CommandRule(r"^(ansible|ansible-playbook)\b.*(-e|--extra-vars)\s+\S*(delete|destroy|absent)", CommandClass.DANGEROUS, PermissionLevel.DESTROY, "ansible destructive", True),
    # ---- CAUTION: mutating, approval depends on environment/policy --------
    CommandRule(r"^git\s+push\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "git push", True),
    CommandRule(r"^git\s+(commit|merge|rebase|tag|checkout\s+-b|switch\s+-c|cherry-pick|revert|stash)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "git write", True),
    CommandRule(r"^docker\s+(build|run|compose\s+(up|build|restart)|push|tag|stop|restart|kill|exec)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "docker mutation", True),
    CommandRule(r"^terraform\s+(apply|import|taint|untaint)\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "terraform apply", True),
    CommandRule(r"^terraform\s+init\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "terraform init (downloads providers)", True),
    CommandRule(r"^kubectl\s+(apply|create|patch|edit|replace|scale|label|annotate|set|cordon|uncordon|taint|exec|cp|port-forward)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "kubectl mutation", True),
    CommandRule(r"^kubectl\s+rollout\s+(restart|undo|resume|pause)\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "kubectl rollout", True),
    CommandRule(r"^helm\s+(install|upgrade|rollback)\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "helm release change", True),
    CommandRule(r"^aws\s+\S+\s+(modify|update|put|create|start|stop|reboot|run|register|associate|attach|tag|set|enable|invoke|restart|copy|reset)[a-z\-]*\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "aws mutation", True),
    CommandRule(r"^aws\s+s3\s+(cp|sync|mv)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "s3 write", True),
    CommandRule(r"^(systemctl|service)\s+(restart|start|stop|reload|enable|disable|mask|unmask)\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "service state change", True),
    CommandRule(r"^(apt|apt-get|yum|dnf|zypper|apk|pip|pip3|npm|brew)\s+(install|remove|purge|upgrade|update|uninstall|autoremove)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "package management", True),
    CommandRule(r"^(useradd|usermod|groupadd|chmod|chown|chgrp|mount|umount|swapon|swapoff|sysctl\s+-w|modprobe)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "system mutation", True),
    CommandRule(r"^(iptables|nft|ufw|firewall-cmd)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "firewall change", True),
    CommandRule(r"^(ansible-playbook)\b(?!.*--check)", CommandClass.CAUTION, PermissionLevel.DEPLOY, "ansible playbook run", True),
    CommandRule(r"^ansible\b.*\s-m\s+(?!setup|ping|command|shell|debug)", CommandClass.CAUTION, PermissionLevel.MODIFY, "ansible module", True),
    CommandRule(r"^(gh|glab)\s+(pr|mr)\s+(create|merge|close|edit|review|comment)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "PR mutation", True),
    CommandRule(r"^(gh|glab)\s+(workflow|run|pipeline|ci)\s+(run|rerun|retry|cancel|dispatch)\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "pipeline trigger", True),
    CommandRule(r"^(argocd|flux)\s+(app\s+sync|sync|reconcile|suspend|resume|rollback)\b", CommandClass.CAUTION, PermissionLevel.DEPLOY, "gitops sync", True),
    CommandRule(r"^(cp|mv|ln|mkdir|touch|tee|sed\s+-i|truncate)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "filesystem write", True),
    CommandRule(r"(>\s*\S|>>\s*\S)", CommandClass.CAUTION, PermissionLevel.MODIFY, "shell redirection writes a file", True),
    CommandRule(r"^(kill|pkill|killall)\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "process kill", True),
    CommandRule(r"^crontab\s+(-r|-e|\S+\.cron|-)", CommandClass.CAUTION, PermissionLevel.MODIFY, "cron mutation", True),
    # ---- SAFE ------------------------------------------------------------
    CommandRule(r"^(ls|pwd|cat|head|tail|less|more|wc|grep|egrep|rg|find|stat|file|du|df|free|top\s+-b|htop|ps|uptime|uname|hostname|id|whoami|date|env\s+\|\s*grep|lsblk|lsof|mount\s*$|journalctl|dmesg|last|w|who|vmstat|iostat|sar|nproc|lscpu|lsmod|getent|readlink|realpath|tree|diff|md5sum|sha256sum|openssl\s+(x509|s_client)|timedatectl|hostnamectl|loginctl|sysctl\s+-a|sysctl\s+[a-z])\b", CommandClass.SAFE, PermissionLevel.READ, "read-only host inspection", True),
    CommandRule(r"^(systemctl|service)\s+(status|list-units|list-unit-files|show|is-active|is-enabled|cat|list-timers|list-dependencies)\b", CommandClass.SAFE, PermissionLevel.READ, "service inspection", True),
    CommandRule(r"^(ip\s+(addr|a|route|r|link|l|neigh|-s)|ss|netstat|dig|nslookup|host|ping\s+-c|traceroute|tracepath|mtr\s+--report|curl\s+(-I|-s|-sS|-v|-o\s*/dev/null|--head|-w)|nc\s+-z|telnet|arp|ethtool\s+\S+$|resolvectl|nmcli\s+(device|connection\s+show|general))\b", CommandClass.SAFE, PermissionLevel.READ, "network inspection", True),
    CommandRule(r"^git\s+(status|diff|log|show|branch(\s+-[avr]+)?|remote\s+-v|remote\s+show|rev-parse|describe|ls-files|blame|shortlog|tag\s+-l|fetch|config\s+--get|ls-remote|check-ignore|cat-file|worktree\s+list|stash\s+list)\b", CommandClass.SAFE, PermissionLevel.READ, "git inspection", True),
    CommandRule(r"^docker\s+(ps|images|logs|inspect|version|info|stats\s+--no-stream|top|port|history|diff|compose\s+(ps|logs|config|version)|network\s+(ls|inspect)|volume\s+(ls|inspect)|system\s+df|events\s+--until)\b", CommandClass.SAFE, PermissionLevel.READ, "docker inspection", True),
    CommandRule(r"^kubectl\s+(get|describe|logs|top|explain|version|cluster-info|api-resources|api-versions|config\s+(view|current-context|get-contexts)|auth\s+can-i|diff|rollout\s+(status|history)|events|wait)\b", CommandClass.SAFE, PermissionLevel.READ, "kubectl inspection", True),
    CommandRule(r"^kubectl\s+(apply|create)\b.*--dry-run", CommandClass.SAFE, PermissionLevel.ANALYZE, "kubectl dry run", True),
    CommandRule(r"^helm\s+(list|ls|status|get|history|show|template|lint|search|version|repo\s+list|dependency\s+list)\b", CommandClass.SAFE, PermissionLevel.READ, "helm inspection", True),
    CommandRule(r"^kustomize\s+build\b", CommandClass.SAFE, PermissionLevel.ANALYZE, "kustomize render", True),
    CommandRule(r"^terraform\s+(plan|validate|fmt\s+-check|fmt\s+-diff|show|output|version|providers|graph|state\s+(list|show|pull)|workspace\s+(list|show))\b", CommandClass.SAFE, PermissionLevel.ANALYZE, "terraform read/plan", True),
    CommandRule(r"^terraform\s+fmt\b", CommandClass.CAUTION, PermissionLevel.MODIFY, "terraform fmt rewrites files", True),
    CommandRule(r"^aws\s+(sts\s+get-caller-identity|\S+\s+(describe|list|get|lookup|search|query|scan|head|test|check|batch-get|filter|simulate)[a-z\-]*)\b", CommandClass.SAFE, PermissionLevel.READ, "aws read-only API", True),
    CommandRule(r"^aws\s+logs\s+(filter-log-events|get-log-events|tail|start-query|get-query-results)\b", CommandClass.SAFE, PermissionLevel.READ, "cloudwatch logs", True),
    CommandRule(r"^aws\s+s3\s+ls\b", CommandClass.SAFE, PermissionLevel.READ, "s3 list", True),
    CommandRule(r"^(ansible-playbook\b.*--check|ansible-inventory|ansible-lint|ansible-doc|ansible-config\s+(dump|view|list)|ansible\b.*-m\s+(setup|ping|debug)\b)", CommandClass.SAFE, PermissionLevel.ANALYZE, "ansible read/check mode", True),
    CommandRule(r"^(trivy|semgrep|gitleaks|checkov|tfsec|snyk\s+test|bandit|hadolint|yamllint|shellcheck|kubeconform|kubeval|conftest|tflint)\b", CommandClass.SAFE, PermissionLevel.ANALYZE, "security/lint scanner", True),
    CommandRule(r"^(gh|glab)\s+(pr|mr|issue|run|workflow|pipeline|ci|repo|api\s+-X\s*GET|api\s+(?!-X)|release|auth\s+status)\s*(list|view|status|checks|diff|log|logs|watch|download|get|trace)?\b(?!.*(create|merge|close|edit|delete|rerun|cancel|run\s+\S+\s+--ref))", CommandClass.SAFE, PermissionLevel.READ, "PR/pipeline inspection", True),
    CommandRule(r"^(argocd|flux)\s+(app\s+(get|list|diff|history|logs)|get|logs|version|check|stats)\b", CommandClass.SAFE, PermissionLevel.READ, "gitops inspection", True),
    CommandRule(r"^(python3?|py)\s+(-m\s+)?(pytest|unittest|black\s+--check|ruff|mypy|flake8|pylint|yaml|json\.tool|py_compile)\b", CommandClass.SAFE, PermissionLevel.ANALYZE, "test/lint runner", True),
    CommandRule(r"^(pytest|npm\s+(test|run\s+(test|lint|build))|yarn\s+(test|lint|build)|go\s+(test|vet|build)|make\s+(test|lint|check)|mvn\s+(test|verify)|gradle\s+(test|check)|cargo\s+(test|check|clippy))\b", CommandClass.SAFE, PermissionLevel.ANALYZE, "test/build runner", True),
    CommandRule(r"^(echo|printf|true|false|type|which|command\s+-v|test|\[)\b", CommandClass.SAFE, PermissionLevel.READ, "shell builtin", True),
    CommandRule(r"^(jq|yq|awk|sed(?!\s+-i)|sort|uniq|cut|tr|column|xargs\s+(echo|grep|cat|ls|wc)|base64\s+-d|base64\s+--decode)\b", CommandClass.SAFE, PermissionLevel.READ, "text processing", True),
]


@dataclass
class Classification:
    command: str
    klass: CommandClass
    permission: PermissionLevel
    reason: str
    matched_rule: Optional[str] = None

    @property
    def requires_approval(self) -> bool:
        return self.klass in (CommandClass.CAUTION, CommandClass.DANGEROUS)

    @property
    def forbidden(self) -> bool:
        return self.klass == CommandClass.FORBIDDEN


class CommandClassifier:
    def __init__(self, extra_rules: Optional[Iterable[CommandRule]] = None, unknown_class: CommandClass = CommandClass.CAUTION) -> None:
        self.rules: list[CommandRule] = list(_BUILTIN_RULES)
        self.unknown_class = unknown_class
        if extra_rules:
            self.add_rules(extra_rules)

    def add_rules(self, rules: Iterable[CommandRule]) -> None:
        """Extra rules are consulted only after built-in FORBIDDEN/DANGEROUS rules.

        A configured rule may make a command *stricter* but can never relax a built-in
        DANGEROUS/FORBIDDEN classification.
        """
        strict = [r for r in self.rules if r.builtin and r.klass in (CommandClass.FORBIDDEN, CommandClass.DANGEROUS)]
        rest = [r for r in self.rules if r not in strict]
        self.rules = strict + list(rules) + rest

    @staticmethod
    def normalise(command: str) -> str:
        cmd = " ".join(command.strip().split())
        # strip leading sudo / env assignments / time
        for prefix in ("sudo -E ", "sudo ", "time ", "nohup "):
            if cmd.startswith(prefix):
                cmd = cmd[len(prefix):]
        while re.match(r"^[A-Z_][A-Z0-9_]*=\S*\s+", cmd):
            cmd = re.sub(r"^[A-Z_][A-Z0-9_]*=\S*\s+", "", cmd)
        return cmd

    def split_pipeline(self, command: str) -> list[str]:
        """Split on shell operators so every segment is classified; the worst wins."""
        parts = re.split(r"\s*(?:\|\||&&|;|\|)\s*", command)
        return [p for p in parts if p.strip()]

    def classify(self, command: str) -> Classification:
        normalised = self.normalise(command)
        # strict rules are matched against the whole command first so pipelines like "curl ... | bash" cannot hide behind splitting
        for rule in self.rules:
            if rule.klass in (CommandClass.FORBIDDEN, CommandClass.DANGEROUS) and rule.regex.search(normalised):
                return Classification(command, rule.klass, rule.permission, rule.reason, rule.pattern)
        worst: Optional[Classification] = None
        rank = {CommandClass.SAFE: 0, CommandClass.CAUTION: 1, CommandClass.DANGEROUS: 2, CommandClass.FORBIDDEN: 3}
        for segment in self.split_pipeline(normalised):
            c = self._classify_segment(segment, normalised)
            if worst is None or rank[c.klass] > rank[worst.klass]:
                worst = c
        if worst is None:
            return Classification(command, CommandClass.SAFE, PermissionLevel.READ, "empty command")
        return Classification(command, worst.klass, worst.permission, worst.reason, worst.matched_rule)

    def _classify_segment(self, segment: str, full: str) -> Classification:
        seg = self.normalise(segment)
        # redirections are evaluated on the full string so "a | b > f" is caught
        for rule in self.rules:
            target = full if rule.pattern.startswith("(>") else seg
            if rule.regex.search(target):
                return Classification(segment, rule.klass, rule.permission, rule.reason, rule.pattern)
        try:
            prog = shlex.split(seg, posix=True)[0] if seg else ""
        except ValueError:
            prog = seg.split(" ")[0]
        return Classification(segment, self.unknown_class, PermissionLevel.MODIFY,
                              f"unrecognised command '{prog}' treated as {self.unknown_class.value}")


def rules_from_config(entries: Iterable[dict]) -> list[CommandRule]:
    rules: list[CommandRule] = []
    for e in entries or []:
        klass = CommandClass(str(e.get("class", "caution")).lower())
        perm = PermissionLevel.parse(e.get("permission", {"safe": "READ", "caution": "MODIFY", "dangerous": "DESTROY", "forbidden": "DESTROY"}[klass.value]))
        rules.append(CommandRule(e["pattern"], klass, perm, e.get("reason", "configured rule")))
    return rules
