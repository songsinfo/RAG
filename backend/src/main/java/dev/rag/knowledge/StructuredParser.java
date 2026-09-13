package dev.rag.knowledge;

import dev.rag.common.*;
import org.jsoup.Jsoup;
import org.jsoup.nodes.Element;
import org.springframework.stereotype.Component;

import java.nio.*;
import java.nio.charset.*;
import java.util.*;

/**
 * 结构化文档语义解析组件。
 * 支持 Markdown、HTML 与 JSONL (FAQ) 的多源解析，提取标题层级、代码块、表格行列与文本段落为统一的解析单元。
 */
@Component
public class StructuredParser {
    /**
     * 结构化解析单元实体 Record。
     *
     * @param key       单元唯一标识（如 line:1 或 paragraph:2）
     * @param kind      单元语义类型（TEXT / HEADING / TABLE / CODE / IMAGE）
     * @param text      提取出的纯文本或 Markdown/表格字符串
     * @param location  位置元数据（段落号、行号范围、所属标题、表格坐标等）
     * @param machine   是否由版面分析引擎机器生成
     * @param imageData 图像 Base64 Data URI（仅 IMAGE 类型非空）
     */
    public record Unit(String key, String kind, String text, Map<String, Object> location, boolean machine, String imageData) { public
        Unit {
            location = new LinkedHashMap<>(location);
        }
    }

    /**
     * 解析结果包装 Record。
     *
     * @param units    解析出的有序单元列表
     * @param warnings 解析过程中的结构缺陷或警告信息列表
     */
    public record Result(List<Unit> units, List<Map<String, Object>> warnings) {
    }

    /**
     * 将二进制字节数组严格按 UTF-8 解码为字符串。
     * 遇非法编码抛出 422 异常，并自动剔除 UTF-8 BOM (\uFEFF)。
     *
     * @param b 待解码的字节数组
     * @return 规整后的 UTF-8 字符串
     * @throws ApiException 若包含非法或非 UTF-8 字节序列（422 ENCODING_INVALID）
     */
    public static String utf8(byte[] b) {
        try {
            return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(b)).toString().replace("\uFEFF", "");
        } catch (CharacterCodingException e) {
            throw new ApiException(422, "ENCODING_INVALID", "文档必须为 UTF-8");
        }
    }

    /**
     * 解析文档通用入口。
     * 统一换行符 (\r\n -> \n) 并根据文件扩展名分发到对应的专用解析器。
     *
     * @param bytes 文件字节数组
     * @param name  文件名（用于后缀嗅探）
     * @return 统一解析结果 Result
     */
    public Result parse(byte[] bytes, String name) {
        String text = utf8(bytes).replace("\r\n", "\n").replace('\r', '\n');
        String lower = name.toLowerCase(Locale.ROOT);
        if (lower.endsWith(".jsonl")) return faq(text);
        if (lower.endsWith(".html") || lower.endsWith(".htm")) return html(text);
        return markdown(text);
    }

    /**
     * 解析 JSONL 格式的 FAQ 问答对数据。
     * 每一行为一个独立 JSON 对象，必须包含非空的 question 和 answer 属性。
     *
     * @param text 规范化后的纯文本
     * @return FAQ 解析结果
     * @throws ApiException 若任意行缺失 question 或 answer（422 FAQ_INVALID）
     */
    private Result faq(String text) {
        List<Unit> result = new ArrayList<>();
        int line = 0;
        for (String raw : text.split("\n")) {
            line++;
            if (raw.isBlank()) continue;
            var row = Json.object(raw);
            String q = Json.str(row, "question"), a = Json.str(row, "answer");
            if (q.isBlank() || a.isBlank())
                throw new ApiException(422, "FAQ_INVALID", "FAQ 第 " + line + " 行缺少 question/answer");
            result.add(new Unit("line:" + line, "TEXT", "问题：" + q + "\n答案：" + a, Map.of("line", line, "faq", true), false, null));
        }
        return checked(result, List.of());
    }

    /**
     * 解析 HTML 页面内容。
     * 使用 Jsoup 剔除 script/style/iframe 等不安全标签，保留标题、段落、预格式化代码及表格结构。
     *
     * @param text HTML 纯文本
     * @return HTML 解析结果单元列表
     */
    private Result html(String text) {
        var doc = Jsoup.parse(text);
        doc.select("script,style,iframe,object,embed,svg").remove();
        List<Unit> units = new ArrayList<>();
        List<Map<String, Object>> warnings = new ArrayList<>();
        int n = 0;
        for (Element e : doc.select("h1,h2,h3,h4,h5,h6,p,pre,table,li,img")) {
            if (e.parents().stream().anyMatch(p -> Set.of("table", "pre", "li").contains(p.tagName()))) continue;
            String kind = e.tagName().matches("h[1-6]") ? "HEADING" : e.tagName().equals("table") ? "TABLE" : e.tagName().equals("pre") ? "CODE" : "TEXT";
            Map<String, Object> loc = new LinkedHashMap<>(Map.of("paragraph", ++n));
            String value = e.wholeText();
            if (e.tagName().equals("img")) {
                warnings.add(Map.of("code", "IMAGE_ATTACHMENT_REQUIRED", "unit", n));
                continue;
            }
            if (kind.equals("TABLE")) {
                var rows = e.select("tr").stream().map(r -> r.select("th,td").eachText()).toList();
                loc.put("rows", rows);
                int headerRows = 0;
                for (Element row : e.select("tr")) {
                    if (row.select("th").isEmpty()) break;
                    headerRows++;
                }
                loc.put("headerRowCount", headerRows);
                loc.put("cells", e.select("th,td").stream().map(c -> Map.of("text", c.text(), "rowspan", c.attr("rowspan"), "colspan", c.attr("colspan"))).toList());
                value = rows.stream().map(r -> String.join(" | ", r)).reduce((a, b) -> a + "\n" + b).orElse("");
            }
            if (!value.isBlank()) units.add(new Unit("paragraph:" + n, kind, value, loc, false, null));
        }
        return checked(units, warnings);
    }

    /**
     * 解析 Markdown 文本。
     * 支持多行代码栅栏 (``` / ~~~)、ATX 标题 (#..######)、Markdown 管道表格 (|) 及段落切分。
     *
     * @param text 规范化后的 Markdown 文本
     * @return Markdown 单元集合与语法告警
     */
    private Result markdown(String text) {
        List<Unit> units = new ArrayList<>();
        List<Map<String, Object>> warnings = new ArrayList<>();
        String[] lines = text.split("\n", -1);
        StringBuilder current = new StringBuilder();
        String kind = "TEXT", heading = "";
        int start = 1;
        boolean code = false;
        String fence = "";
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            boolean marker = line.stripLeading().startsWith("```") || line.stripLeading().startsWith("~~~");
            if (marker) {
                if (!code) {
                    flush(units, current, kind, start, i, heading);
                    kind = "CODE";
                    start = i + 1;
                    fence = line.stripLeading().substring(0, 3);
                    code = true;
                    current.append(line).append('\n');
                } else {
                    current.append(line).append('\n');
                    if (line.stripLeading().startsWith(fence)) {
                        flush(units, current, kind, start, i + 1, heading);
                        code = false;
                        kind = "TEXT";
                        start = i + 2;
                    }
                }
                continue;
            }
            if (code) {
                current.append(line).append('\n');
                continue;
            }
            if (line.matches("^#{1,6}\\s+.*")) {
                flush(units, current, kind, start, i, heading);
                heading = line.replaceFirst("^#+\\s+", "");
                units.add(new Unit("line:" + (i + 1), "HEADING", heading, Map.of("lineStart", i + 1, "lineEnd", i + 1, "heading", heading), false, null));
                start = i + 2;
                kind = "TEXT";
            } else if (line.isBlank()) {
                flush(units, current, kind, start, i, heading);
                start = i + 2;
                kind = "TEXT";
            } else {
                if (current.isEmpty()) start = i + 1;
                if (line.contains("|") && (kind.equals("TABLE") || i + 1 < lines.length && lines[i + 1].matches("^[ |:.-]+$")))
                    kind = "TABLE";
                current.append(line).append('\n');
                if (line.contains("![")) warnings.add(Map.of("code", "IMAGE_ATTACHMENT_REQUIRED", "line", i + 1));
            }
        }
        flush(units, current, kind, start, lines.length, heading);
        if (code) warnings.add(Map.of("code", "UNCLOSED_CODE_FENCE", "line", start));
        return checked(units, warnings);
    }

    /**
     * 将当前累积的文本行缓冲区作为独立单元写入单元列表并清空。
     *
     * @param units   目标单元列表
     * @param text    当前文本构建器
     * @param kind    单元类型
     * @param start   起始行号
     * @param end     结束行号
     * @param heading 当前最近的父级标题文本
     */
    private void flush(List<Unit> units, StringBuilder text, String kind, int start, int end, String heading) {
        String value = text.toString().stripTrailing();
        if (!value.isBlank())
            units.add(new Unit("line:" + start, kind, value, Map.of("lineStart", start, "lineEnd", end, "heading", heading), false, null));
        text.setLength(0);
    }

    /**
     * 校验最终解析单元的非空性与条数上限（单文档最多 10000 单元）。
     *
     * @param units    解析单元列表
     * @param warnings 警告列表
     * @return 校验通过的包装 Result
     * @throws ApiException 若文档解析结果为空（422 EMPTY_DOCUMENT）或超过 10000 条（413 INPUT_TOO_LARGE）
     */
    private Result checked(List<Unit> units, List<Map<String, Object>> warnings) {
        if (units.isEmpty()) throw new ApiException(422, "EMPTY_DOCUMENT", "文档没有可读内容");
        if (units.size() > 10000) throw new ApiException(413, "INPUT_TOO_LARGE", "来源单元超过上限");
        return new Result(units, warnings);
    }
}
