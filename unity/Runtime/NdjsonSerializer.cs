using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Playtest.Telemetry
{
    /// <summary>
    /// 이벤트를 NDJSON 한 줄로 직렬화한다. 워커 스레드에서만 호출된다.
    /// StringBuilder 를 재사용하여 줄마다 할당이 생기지 않게 한다.
    /// </summary>
    internal sealed class NdjsonSerializer
    {
        private readonly StringBuilder _sb = new StringBuilder(512);

        public string Serialize(TelemetryEvent e)
        {
            _sb.Clear();
            _sb.Append('{');

            WriteString("session_id", e.SessionId, first: true);
            WriteString("player_id", e.PlayerId);
            WriteNumber("seq", e.Seq);
            WriteNumber("t_ms", e.TMs);
            WriteNumber("t_raw_ms", e.TRawMs);
            WriteString("wall_clock", e.WallClockUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffZ",
                                                              CultureInfo.InvariantCulture));
            WriteString("name", e.Name);

            if (!string.IsNullOrEmpty(e.Tag))
            {
                WriteString("tag", e.Tag);
            }

            WriteString("tier", e.Tier == EventTier.Sampled ? "sampled" : "critical");
            WriteString("build_version", e.BuildVersion);

            if (e.Params.Count > 0)
            {
                _sb.Append(",\"params\":{");
                for (int i = 0; i < e.Params.Count; i++)
                {
                    if (i > 0) _sb.Append(',');
                    AppendQuoted(e.Params[i].Key);
                    _sb.Append(':');
                    AppendValue(e.Params[i].Value);
                }
                _sb.Append('}');
            }

            _sb.Append('}');
            return _sb.ToString();
        }

        private void WriteString(string key, string value, bool first = false)
        {
            if (!first) _sb.Append(',');
            AppendQuoted(key);
            _sb.Append(':');
            if (value == null) _sb.Append("null");
            else AppendQuoted(value);
        }

        private void WriteNumber(string key, long value)
        {
            _sb.Append(',');
            AppendQuoted(key);
            _sb.Append(':');
            _sb.Append(value.ToString(CultureInfo.InvariantCulture));
        }

        /// <summary>
        /// 허용 타입은 설계서 2.7 의 int, long, float, bool, string 에 더해, 나머지
        /// 정수·실수 타입(uint, ulong, ushort, sbyte, short, byte, double, decimal)이다.
        /// 숫자는 전부 JSON 숫자로 쓴다. 문자열로 쓰면 분석 쪽에서 글자로 보관되어
        /// sum · avg 지표에서 오류 없이 빠진다.
        ///
        /// 2^53 을 넘는 ulong(Steam ID 등)도 숫자로 쓴다. 적재기가 그런 값을 정밀도를
        /// 잃지 않도록 글자로 보관한다.
        ///
        /// 그 외 타입(열거형 등)은 문자열로 변환한다(기록 시점에 경고가 출력된다).
        /// </summary>
        private void AppendValue(object v)
        {
            if (v == null) { _sb.Append("null"); return; }

            switch (v)
            {
                case string s:
                    AppendQuoted(s);
                    return;
                case bool b:
                    _sb.Append(b ? "true" : "false");
                    return;
                case int i:
                    _sb.Append(i.ToString(CultureInfo.InvariantCulture));
                    return;
                case long l:
                    _sb.Append(l.ToString(CultureInfo.InvariantCulture));
                    return;
                case float f:
                    AppendFloat(f);
                    return;
                case double d:
                    AppendDouble(d);
                    return;
                case short sh:
                    _sb.Append(((int)sh).ToString(CultureInfo.InvariantCulture));
                    return;
                case byte by:
                    _sb.Append(((int)by).ToString(CultureInfo.InvariantCulture));
                    return;
                case sbyte sb8:
                    _sb.Append(((int)sb8).ToString(CultureInfo.InvariantCulture));
                    return;
                case ushort us:
                    _sb.Append(((int)us).ToString(CultureInfo.InvariantCulture));
                    return;
                case uint ui:
                    _sb.Append(ui.ToString(CultureInfo.InvariantCulture));
                    return;
                case ulong ul:
                    _sb.Append(ul.ToString(CultureInfo.InvariantCulture));
                    return;
                case decimal m:
                    // decimal 은 지수 표기를 쓰지 않으므로 그대로 JSON 숫자가 된다.
                    _sb.Append(m.ToString(CultureInfo.InvariantCulture));
                    return;
                default:
                    AppendQuoted(Convert.ToString(v, CultureInfo.InvariantCulture));
                    return;
            }
        }

        /// <summary>
        /// float 는 float 인 채로 쓴다. double 로 넓힌 뒤 쓰면 33.04f 가
        /// 33.040000915527344 로 남는다 — 보기 나쁘고, 분석 쪽에서 0.1 과 같은지
        /// 비교하는 조건이 맞지 않게 된다.
        /// </summary>
        private void AppendFloat(float f)
        {
            if (float.IsNaN(f) || float.IsInfinity(f))
            {
                _sb.Append("null");
                return;
            }
            _sb.Append(f.ToString("R", CultureInfo.InvariantCulture));
        }

        private void AppendDouble(double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d))
            {
                // JSON 에는 NaN/Infinity 표현이 없으므로 null 로 기록한다.
                _sb.Append("null");
                return;
            }
            _sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
        }

        private void AppendQuoted(string s)
        {
            _sb.Append('"');
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                switch (c)
                {
                    case '"': _sb.Append("\\\""); break;
                    case '\\': _sb.Append("\\\\"); break;
                    case '\n': _sb.Append("\\n"); break;
                    case '\r': _sb.Append("\\r"); break;
                    case '\t': _sb.Append("\\t"); break;
                    case '\b': _sb.Append("\\b"); break;
                    case '\f': _sb.Append("\\f"); break;
                    default:
                        if (c < 0x20)
                        {
                            _sb.Append("\\u");
                            _sb.Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            _sb.Append(c);
                        }
                        break;
                }
            }
            _sb.Append('"');
        }
    }
}
