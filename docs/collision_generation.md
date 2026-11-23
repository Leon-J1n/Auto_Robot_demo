# 可复现碰撞几何生成与交付标准

本文定义碰撞几何如何从 OLS 资产生成、审核并交给运行时。目标是让新机器、
新资产和新依赖都能重现或明确拒绝，而不是依赖某台机器的 `.cache`。

## 1. 生成输入必须可追溯

每次生成建立独立的 delivery 目录，不覆盖旧目录。目录至少记录：

- 生成器仓库 commit 和工作树是否干净；
- 生成镜像 digest、Python 版本、操作系统/架构；
- CoACD、trimesh、fast-simplification、numpy、scipy、MuJoCo 版本；
- 生成参数的规范 JSON（排序键、固定浮点格式和随机 seed）；
- OLS 版本、资产索引版本和每个源 OBJ/URDF/MTL/纹理的 SHA256；
- 输入 Scene、Commands 及其 SHA256。

源资产先复制到只读 staging 目录，再生成。任何输入在生成期间改变都使本次
交付失效，不能用新的文件覆盖 manifest 中的哈希。

## 2. 统一生成环境

优先使用交付包中的 `generation/Dockerfile` 和锁定的
`generation/requirements-generation.txt`。生成命令只接受 staging 输入和显式
参数，不读取宿主机 `.cache`。容器构建后记录镜像 digest；不能只记录一个可变
tag。对于 Windows/Linux 或 CPU 指令集不同的结果，必须分别生成并审核，不能
把“参数相同”当成几何字节相同。

生成器对每个 link/模型输出：

1. 源资产哈希和缩放；
2. 简化和凸分解参数；
3. 凸块数量、体积、OBJ SHA256；
4. 生成器和依赖版本；
5. 审核所需的碰撞对、豁免和场景复现信息。

输出先写临时目录，完成全部校验后原子改名为 delivery 目录。禁止直接把半成品
写进运行时 `.cache`。

交付包中的生成器是候选产物生成入口，例如：

```bash
docker build --pull -t collision-generator:<commit> generation
docker run --rm --read-only \
  -v /path/staging-assets:/assets:ro \
  -v /path/jobs.json:/jobs.json:ro \
  -v /path/generator-source:/generator:ro \
  -v /path/new-delivery:/out \
  collision-generator:<commit> \
  --jobs /jobs.json --asset-root /assets --generator-root /generator --output /out
```

`--output` 必须指向不存在的新目录；输出的 `generation_report.json`、作业文件和
源哈希是后续审核的输入。容器标签中的 `<commit>` 只是示意，实际交付还要把
镜像 digest 写入报告。

## 3. 交付包验收门槛

运行独立校验器（它不导入控制项目）：

```bash
python tools/verify_collision_bundle.py /path/to/delivery
```

校验器必须检查 manifest、所有 content-addressed OBJ、缓存证据、源指纹和便携
URDF。缺文件、哈希不一致、路径穿越或 manifest 不完整均 fail closed。

随后由控制端审核：

- FCL/MuJoCo 能加载便携 URDF；
- 关键 link 的凸块数量和尺寸在允许范围；
- 自碰撞、环境碰撞、夹爪豁免和携带物体规则符合策略；
- 参考抓放场景的直接路径、避障路径和拒绝用例结果符合预期；
- 运行时生成的 fingerprint 与交付包完全一致。

只有这些检查完成后，维护者才把 `review_status` 改为 `verified`。未审核包只能
通过 `COLLISION_BUNDLE_ALLOW_UNREVIEWED=1` 做隔离复现，不能部署给正常服务。

## 4. 新资产流程

新增或更新 OLS 模型时：

1. 生成新的资产目录和输入指纹，不修改旧 delivery；
2. 检查 OLS 的尺度、坐标系、材质/网格入口和 URDF 引用；
3. 按相同容器和参数生成；
4. 为新资产增加至少一个正向、一个真实碰撞和一个安全拒绝场景；
5. 运行独立校验器和控制端审核；
6. 更新资产覆盖清单、bundle ID 和部署配置；
7. 通过回归后再将新包标记为 `verified`。

旧包继续可回放。若新资产没有对应 bundle 记录，运行时会返回
`collision_bundle_asset_missing`，不会偷偷调用本机 CoACD 或使用旧缓存。

## 5. 运行时规则

运行时通过 `COLLISION_BUNDLE_ROOT` 选择唯一交付包。包一旦选中：

- `mesh.py` 只加载包内哈希匹配的凸块；
- `robot_visual.py` 只加载包内哈希匹配的便携 URDF；
- 缺失、篡改或源资产哈希变化立即失败；
- 不使用宿主机旧 `.cache`，不进行现场重新分解；
- 日志记录 bundle ID、源资产哈希和 package fingerprint。

这样可以明确区分“资产/生成包问题”和“规划算法问题”，并保证新机器不会在
无意中生成与已审核结果不同的碰撞几何。
