#!/bin/bash
# 假 JMeter 二进制：写一份合规的 log 和 jtl，stdout 输出"成功"格式
# 用法：当作 $JMETER_BIN_HOME/jmeter 安装

LOG_PATH=""
JTL_PATH=""
DATA_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -j)
            LOG_PATH="$2"
            shift 2
            ;;
        -l)
            JTL_PATH="$2"
            shift 2
            ;;
        -o)
            DATA_DIR="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# 写一行 INFO summary 行到 log（满足 testcase getJMeterResult 的正则）
if [[ -n "$LOG_PATH" ]]; then
    mkdir -p "$(dirname "$LOG_PATH")"
    echo "2026-05-12 10:00:00,123 INFO o.a.j.r.Summariser: summary +     100 in 00:00:01 =  100.0/s Avg:     5 Min:     1 Max:    10 Err:     0 (0.00%)" > "$LOG_PATH"
fi

# 写 jtl 文件：debug 模式（.xml）需要包含 responseData
if [[ "$JTL_PATH" == *.xml ]]; then
    mkdir -p "$(dirname "$JTL_PATH")"
    cat > "$JTL_PATH" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
  <httpSample t="5" lt="3" ts="1715472000123" s="true" lb="http-1" rc="200" rm="OK">
    <responseData class="java.lang.String">Hello from fake JMeter</responseData>
  </httpSample>
</testResults>
EOF
fi

# 写一个最小 HTML 报告目录（run 模式 -e -o）
if [[ -n "$DATA_DIR" ]]; then
    mkdir -p "$DATA_DIR"
    echo "<html>fake report</html>" > "$DATA_DIR/index.html"
fi

# stdout：不要触发 summary=0 / Err: [1-9] / Error.*Exception 任一
echo "summary +     100 in 00:00:01 =  100.0/s Avg:     5 Min:     1 Max:    10 Err:     0 (0.00%)"
exit 0
