# 生育待遇统一结算核心

本项目定义生育待遇案件、费用类别和账本动作的公共格式，帮助医院上传、规则计算和资金处理模块共享稳定语义。金额以最小货币单位的整数表示，避免浮点计算差异。

## 开发与检查

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

运行构建检查：

```bash
python3 -m compileall -q src
```
