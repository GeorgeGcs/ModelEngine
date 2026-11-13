# 课题专用 CI

仓库根目录 `.gitcode/workflows/topic16-ci.yml` 只在本课题路径变化时触发，支持 `topic-16-portfolio-risk` 的 push、面向 main 的 PR，以及手动触发。只申请仓库和 PR 读取权限，不注入服务器或模型凭据，不评论、不发布。目录和插件按 [AtomGit Python CI 示例](https://docs.atomgit.com/en/docs/help/home/org_project/pipeline/examples/python-ci/) 配置。

流水线固定 Python 3.11、项目 MCP 依赖 2.3.0、Bandit 1.9.4，执行 Python 编译、项目单元测试和高危静态扫描，并留存报告。patched Linux observation 另在真实 runtime 执行，不与项目 SDK 混装。

本地等效入口，从课题目录执行，报告目录需是新目录。

```bash
python -B docs/delivery/ci.py --output .ci-results/unit
python -m bandit -r . --ignore-nosec --severity-level high -f json -o .ci-results/security.json
```

本次 Windows 项目回归 401/401、0 skip、退出 0。首次 CI 包装器未固定 UTF-8，舆情测试的子进程错误输出触发编码错误；补齐 `PYTHONUTF8=1` 和 `PYTHONIOENCODING=utf-8` 后通过。共享业务代码未因此改动。两次日志分别保留，不将首次失败改写。

Bandit 遍历全课题 Python 文件，包括测试。高危 0、扫描错误 0，另有中危 15、低危 97；本报告只声称通过高危静态检查门槛，不等于全部风险已排除或依赖漏洞审计已完成。

本地结果见 [回归报告](ci-local-results.json) 和 [扫描摘要](security-local-results.json)。平台是否实际执行，以 PR 检查任务、流水线 run/job ID、对应提交 SHA 和最终状态为准。配置文件存在或 PR 合并标记为绿色，均不能代替真实 job 回执。

[AtomGit 快速开始](https://docs.atomgit.com/docs/help/home/org_project/pipeline/quick-start/)列有代码化流水线内测资格前提。若提交后没有生成任务，应由仓库管理员检查 Actions 是否开通、Runner 是否可用以及 fork PR 的运行权限。不要为了触发任务授予 fork 写权限或注入服务器密钥。

Linux ARM64 上也已从真实 Git 提交 `8ddc0eee0f8b48e4f28f3571f202163bf0718d1d` 独立检出，完成 401/401、0 skip、退出 0。[Linux 全量结果](ci-linux-results.json)。初次只传源码归档缺少 Git 元数据，随后补取对象误用子目录，再发现源仓库为浅克隆；三次准备失败均保留。已按用户约定反馈，查清浅克隆前置提交后采用 `--update-shallow` 和真实基线引用完成隔离检出，全部文件与原归档相同，没有伪造 SHA 或绕过测试。

推送后的公开 API 仍显示两个仓库均 0 个工作流、0 次运行，PR 检查任务 0，详见 [平台状态](ci-hosted-status.json)。仓库 CI 的正式回执目前受阻，需要管理员开通或注册工作流；本地 Linux 成功不替代平台 job。
