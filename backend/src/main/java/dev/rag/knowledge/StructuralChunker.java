package dev.rag.knowledge;


import java.util.*;

/**
 * 层次感知语义分块算法核心组件（Parent-Child Hierarchical Chunker）。
 * <p>
 * <b>算法核心思想：</b>
 * 1. <b>大纲层级隔离：</b>以顶级标题（HEADING）为天然语义断点，独立划分章节，严防跨章节内容粘连；
 * 2. <b>自然语义边界对齐：</b>在子块切分时优先寻找句末标点（换行符 \n、句号。、分号；），避免硬截断破坏上下文语义；
 * 3. <b>表格智能切分与表头保活：</b>针对超长表格，自动提取表头（含 Docling OCR 表头及 Markdown 分隔线），在每个切分出的子表中重复注入表头，保证下游 LLM 具备行字段语境；
 * 4. <b>父子双层块绑定（Parent-Child Context）：</b>检索阶段以紧凑的子块（Child Chunk）计算向量相似度与关键词得分，问答生成阶段回溯其关联的父块（Parent Chunk）作为宽上下文，兼顾召回精度与生成完整度。
 * <p>
 * <i>注：分块窗口以 Unicode Code Point 为准，提供稳定的长度预估。</i>
 */
public final class StructuralChunker {
    /**
     * 文本块在源解析单元中的字符位置偏移区间（用于高亮溯源与引用标注）。
     *
     * @param unitKey 所属 source_unit 唯一标识
     * @param start   在源单元文本中的起始 Code Point 索引
     * @param end     在源单元文本中的结束 Code Point 索引
     */
    public record Span(String unitKey, int start, int end) {
    }

    /**
     * 分块实体 Record。
     *
     * @param key    分块在文档构建中的全局唯一标识（如 "0-c1" 或 "0-p0"）
     * @param parent 父块唯一标识（子块指向对应父块，父块自身为 null）
     * @param kind   分块类型（CHILD 子块 / PARENT 父块）
     * @param text   分块实际文本内容（已拼接表头或子块集合）
     * @param spans  覆盖的原始单元位置溯源列表
     */
    public record Chunk(String key, String parent, String kind, String text, List<Span> spans) {
    }

    /**
     * 对解析单元列表执行完整的层级分块。
     * <p>
     * <b>处理阶段：</b>
     * 1. 验证参数合法性（childCp >= 64, parentCp >= childCp）；
     * 2. 遍历 units，以 HEADING（标题）单元为自然边界切割为独立的章节组；
     * 3. 对每个章节组调用 add 方法分别生成子块集合与父块包裹。
     *
     * @param units    结构化解析器产出的单元列表
     * @param childCp  子块最大 Code Point 字符长度上限
     * @param parentCp 父块最大 Code Point 字符长度上限
     * @return 分块结果列表（包含 PARENT 与关联的 CHILD 块）
     * @throws IllegalArgumentException 若 childCp &lt; 64 或 parentCp &lt; childCp
     */
    public static List<Chunk> chunk(List<StructuredParser.Unit> units, int childCp, int parentCp) {
        if (childCp < 64 || parentCp < childCp) throw new IllegalArgumentException("Invalid chunk limits");
        List<Chunk> out = new ArrayList<>();
        List<StructuredParser.Unit> section = new ArrayList<>();
        int n = 0;
        for (var unit : units) {
            if (unit.kind().equals("HEADING") && !section.isEmpty()) {
                add(out, section, n++, childCp, parentCp);
                section.clear();
            }
            section.add(unit);
        }
        if (!section.isEmpty()) add(out, section, n, childCp, parentCp);
        return out;
    }

    /**
     * 将单一章节内的单元处理为子块并聚合为父块。
     * <p>
     * <b>分块算法细节：</b>
     * 1. 遇到超长表格（kind=TABLE 且字符数 &gt; childCp）时，移交专用 table 方法执行保留表头的行级拆分；
     * 2. 对于原子单元（短表格、代码块、FAQ 对），整体作为一个独立块保留；
     * 3. 对于普通文本段落，按窗口步进，并在窗口后半段（&gt; length/2）优先对齐换行、句号、分号作为自然断句点；
     * 4. 收集当前章节产生的所有子块，按 parentCp 上限贪心合并，生成 PARENT 块并回填子块的 parent 引用。
     *
     * @param out      全局输出列表
     * @param units    当前章节内的单元集合
     * @param section  章节序号
     * @param childCp  子块字符上限
     * @param parentCp 父块字符上限
     */
    private static void add(List<Chunk> out, List<StructuredParser.Unit> units, int section, int childCp, int parentCp) {
        List<Chunk> children = new ArrayList<>();
        int index = 0;
        for (var u : units) {
            int count = u.text().codePointCount(0, u.text().length());
            if (u.kind().equals("TABLE") && count > childCp) {
                for (var part : table(u, childCp))
                    children.add(new Chunk(section + "-c" + index++, null, "CHILD", part.text(), part.spans()));
                continue;
            }
            boolean atomic = u.kind().equals("TABLE") || u.kind().equals("CODE") || Boolean.TRUE.equals(u.location().get("faq"));
            int step = atomic ? count : childCp;
            for (int s = 0; s < count; ) {
                int end = Math.min(count, s + step);
                if (!atomic && end < count) {
                    String window = slice(u.text(), s, end);
                    int boundary = Math.max(window.lastIndexOf('\n'), Math.max(window.lastIndexOf('。'), window.lastIndexOf('；')));
                    if (boundary > window.length() / 2) end = s + window.codePointCount(0, boundary + 1);
                }
                String text = slice(u.text(), s, end);
                children.add(new Chunk(section + "-c" + index++, null, "CHILD", text, List.of(new Span(u.key(), s, end))));
                s = end;
            }
        }
        int group = 0;
        List<Chunk> batch = new ArrayList<>();
        int count = 0;
        for (var c : children) {
            int size = c.text().codePointCount(0, c.text().length());
            if (!batch.isEmpty() && count + size > parentCp) {
                parent(out, batch, section + "-p" + group++);
                batch.clear();
                count = 0;
            }
            batch.add(c);
            count += size;
        }
        if (!batch.isEmpty()) parent(out, batch, section + "-p" + group);
    }

    /**
     * 将一批连续的子块聚合生成一个 PARENT 父块，并建立父子索引关联。
     *
     * @param out      全局输出列表
     * @param children 归属该父块的一组子块
     * @param key      父块唯一标识
     */
    private static void parent(List<Chunk> out, List<Chunk> children, String key) {
        out.add(new Chunk(key, null, "PARENT", String.join("\n", children.stream().map(Chunk::text).toList()), children.stream().flatMap(c -> c.spans().stream()).toList()));
        for (var c : children) out.add(new Chunk(c.key(), key, c.kind(), c.text(), c.spans()));
    }

    /**
     * 基于 Unicode Code Point 索引安全截取子字符串。
     * 正确处理 Emoji、多字节特殊字符与代理对，防止截断半个字符导致乱码。
     *
     * @param text 原始文本
     * @param s    起始 Code Point 偏移量
     * @param e    结束 Code Point 偏移量
     * @return 截取后的子字符串
     */
    public static String slice(String text, int s, int e) {
        return text.substring(text.offsetByCodePoints(0, s), text.offsetByCodePoints(0, e));
    }

    /**
     * 行范围实体，记录行在表格中的 Code Point 起止及文本。
     */
    private record Row(int start, int end, String text) {
    }

    /**
     * 超长表格智能行切分算法（保留表头）。
     * <p>
     * <b>核心流程：</b>
     * 1. 识别表格类型：支持 Docling 单元格坐标格式 ([r0c0]...) 与标准 Markdown 管道格式；
     * 2. 解析表头行数（headerRows）：综合 Markdown 分隔行特征与元数据中的 headerRowCount/column_header 标识；
     * 3. 提取通用表头文本（header）；
     * 4. 遍历数据行，在不超过 max 字符限制的前提下打包多行数据，并在每个子块头部拼接触发的完整表头；
     * 5. 若表头自身或任意单行数据加上表头超过 max，直接抛出 422 TABLE_ROW_TOO_LARGE 熔断。
     *
     * @param unit 表格解析单元
     * @param max  子块字符上限
     * @return 分割后的表格子块列表
     * @throws dev.rag.common.ApiException 若表头或单行超出分块上限
     */
    private static List<Chunk> table(StructuredParser.Unit unit, int max) {
        List<Row> lines = new ArrayList<>();
        int offset = 0;
        for (String line : unit.text().split("(?<=\\n)", -1))
            if (!line.isEmpty()) {
                int end = offset + line.codePointCount(0, line.length());
                lines.add(new Row(offset, end, line));
                offset = end;
            }
        List<Row> rows = new ArrayList<>();
        boolean docling = !lines.isEmpty() && lines.getFirst().text().matches("(?s)^\\[r\\d+c\\d+].*");
        if (docling) {
            int current = -1;
            Row accumulated = null;
            for (var line : lines) {
                var matcher = java.util.regex.Pattern.compile("^\\[r(\\d+)c\\d+]").matcher(line.text());
                if (!matcher.find()) throw tableLimit();
                int row = Integer.parseInt(matcher.group(1));
                if (row == current && accumulated != null)
                    accumulated = new Row(accumulated.start(), line.end(), accumulated.text() + line.text());
                else {
                    if (accumulated != null) rows.add(accumulated);
                    accumulated = line;
                    current = row;
                }
            }
            if (accumulated != null) rows.add(accumulated);
        } else rows.addAll(lines);
        int headerRows = 1;
        if (!docling && rows.size() > 1 && rows.get(1).text().strip().matches("[| :\\-]+")) headerRows = 2;
        Object explicit = unit.location().get("headerRowCount");
        if (explicit instanceof Number n) headerRows = Math.max(headerRows, n.intValue());
        if (docling && unit.location().get("table") instanceof Map<?, ?> metadata && metadata.get("table_cells") instanceof List<?> cells) {
            for (Object item : cells)
                if (item instanceof Map<?, ?> cell && Boolean.TRUE.equals(cell.get("column_header")) && cell.get("end_row_offset_idx") instanceof Number n)
                    headerRows = Math.max(headerRows, n.intValue());
        }
        headerRows = Math.min(headerRows, rows.size());
        int headerEnd = rows.get(headerRows - 1).end();
        String header = slice(unit.text(), 0, headerEnd);
        if (headerEnd >= max || headerRows == rows.size()) throw tableLimit();
        List<Chunk> chunks = new ArrayList<>();
        int start = -1, end = -1, size = headerEnd;
        for (int i = headerRows; i < rows.size(); i++) {
            Row row = rows.get(i);
            int length = row.end() - row.start();
            if (length + headerEnd > max) throw tableLimit();
            if (start >= 0 && size + length > max) {
                chunks.add(tablePart(unit, header, headerEnd, start, end));
                start = -1;
                size = headerEnd;
            }
            if (start < 0) start = row.start();
            end = row.end();
            size += length;
        }
        if (start >= 0) chunks.add(tablePart(unit, header, headerEnd, start, end));
        return chunks;
    }

    /**
     * 构造携带表头的表格切片 Chunk 实体。
     *
     * @param unit      源表格单元
     * @param header    表头文本
     * @param headerEnd 表头结束偏移
     * @param start     数据行起始偏移
     * @param end       数据行结束偏移
     * @return 包含表头 Span 与数据行 Span 的 Chunk 实体
     */
    private static Chunk tablePart(StructuredParser.Unit unit, String header, int headerEnd, int start, int end) {
        return new Chunk("", null, "CHILD", header + slice(unit.text(), start, end), List.of(new Span(unit.key(), 0, headerEnd), new Span(unit.key(), start, end)));
    }

    /**
     * 构造 422 TABLE_ROW_TOO_LARGE 表格行超限异常。
     *
     * @return ApiException 实例
     */
    private static dev.rag.common.ApiException tableLimit() {
        return new dev.rag.common.ApiException(422, "TABLE_ROW_TOO_LARGE", "表头或单行超过分块上限，需拆分原表或调整构建长度；不能忽略后发布");
    }

    private StructuralChunker() {
    }
}
