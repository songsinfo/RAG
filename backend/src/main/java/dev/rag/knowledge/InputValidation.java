package dev.rag.knowledge;

import dev.rag.common.*;
import org.apache.pdfbox.Loader;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.zip.*;

/**
 * 文件上传安全性与合规性校验工具类。
 * 负责限制文件体积、格式白名单校验、Zip Slip 穿越攻击防范、Zip 炸弹防御及格式深度检验（PDF/DOCX/UTF-8）。
 */
public final class InputValidation {
    /**
     * 校验上传文档的安全性、文件类型合法性与结构完整性。
     * <p>
     * <b>校验规则：</b>
     * 1. 基础限制：文件非空且不超过 25 MiB；
     * 2. 格式白名单：支持 md, markdown, txt, jsonl, pdf, docx, html, htm, zip；
     * 3. ZIP 校验：仅支持 .wiki.zip 命名，并验证其可由 WikiPackage 正确解析；
     * 4. PDF 校验：校验 %PDF- 魔数标头、禁止加密文档、页数不可超过 200 页；
     * 5. DOCX 校验：解压容器校验 word/document.xml 存在性，并阻止携带外部 TargetMode="External" 的潜在 SSRF/外链实体；
     * 6. 纯文本文档：校验 UTF-8 字符集编码合法性。
     *
     * @param bytes 文件字节流
     * @param name  原始文件名
     * @return 经过校验的小写文件扩展名
     * @throws ApiException 若文件超限、格式非法、包含恶意结构或内容损坏
     */
    public static String validate(byte[] bytes, String name) {
        if (bytes.length == 0) throw new ApiException(422, "EMPTY_DOCUMENT", "文件为空");
        if (bytes.length > 25 * 1024 * 1024) throw new ApiException(413, "INPUT_TOO_LARGE", "单文件最多 25 MiB");
        String ext = name.substring(name.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT);
        if (!Set.of("md", "markdown", "txt", "jsonl", "pdf", "docx", "html", "htm", "zip").contains(ext))
            throw new ApiException(422, "PARSE_UNSUPPORTED", "不支持该文件格式");
        if (ext.equals("zip")) {
            if (!name.endsWith(".wiki.zip")) throw invalid();
            WikiPackage.parse(bytes, new StructuredParser());
        } else if (ext.equals("pdf")) {
            if (!new String(bytes, 0, Math.min(5, bytes.length), StandardCharsets.US_ASCII).equals("%PDF-"))
                throw invalid();
            try (var pdf = Loader.loadPDF(bytes)) {
                if (pdf.isEncrypted() || pdf.getNumberOfPages() > 200)
                    throw new ApiException(422, "PDF_LIMIT", "PDF 加密或超过 200 页");
            } catch (IOException e) {
                throw invalid();
            }
        } else if (ext.equals("docx")) {
            var files = archive(bytes);
            if (!files.containsKey("word/document.xml")) throw invalid();
            for (var e : files.entrySet())
                if (e.getKey().endsWith(".rels") && new String(e.getValue(), StandardCharsets.UTF_8).matches("(?s).*TargetMode\\s*=\\s*[\"']External[\"'].*"))
                    throw new ApiException(422, "REMOTE_RESOURCE_BLOCKED", "DOCX 包含外部关系，请导出本地副本");
        } else StructuredParser.utf8(bytes);
        return ext;
    }

    /**
     * 安全解压并提取 ZIP 压缩包中的文件列表。
     * <p>
     * <b>安全防护：</b>
     * 1. Zip Slip 防御：严禁根路径 (/)、反斜杠 (\\)、冒号 (:) 及路径穿越 (..)；
     * 2. 条目数量限制：单包最多 1000 个条目；
     * 3. Zip 炸弹防御：单个解压条目不超过 25 MiB，解压总膨胀体积不超过 250 MiB，且膨胀率不超过 100 倍；
     * 4. 递归压缩包阻断：禁止包内包含嵌套 .zip 文件。
     *
     * @param bytes 压缩包字节流
     * @return 路径与文件字节数组的 Map 映射
     * @throws ApiException 若发现路径穿越、炸弹特征或结构损坏
     */
    public static Map<String, byte[]> archive(byte[] bytes) {
        Map<String, byte[]> files = new LinkedHashMap<>();
        long total = 0;
        try (var zip = new ZipInputStream(new ByteArrayInputStream(bytes))) {
            ZipEntry e;
            int count = 0;
            while ((e = zip.getNextEntry()) != null) {
                if (++count > 1000) throw invalid();
                String name = e.getName();
                if (name.startsWith("/") || name.contains("\\") || name.contains(":") || Arrays.asList(name.split("/")).contains("..") || name.toLowerCase(Locale.ROOT).endsWith(".zip"))
                    throw invalid();
                if (e.isDirectory()) continue;
                byte[] data = zip.readNBytes(25 * 1024 * 1024 + 1);
                if (data.length > 25 * 1024 * 1024 || (total += data.length) > 250L * 1024 * 1024 || total > Math.max(1, bytes.length) * 100L)
                    throw new ApiException(413, "ARCHIVE_LIMIT", "压缩包超过安全限制");
                if (files.put(name, data) != null) throw invalid();
            }
            return files;
        } catch (IOException e) {
            throw invalid();
        }
    }

    /**
     * 构造 422 FILE_INVALID 文件内容或容器结构无效异常。
     *
     * @return ApiException 实例
     */
    private static ApiException invalid() {
        return new ApiException(422, "FILE_INVALID", "文件内容或容器结构无效");
    }

    private InputValidation() {
    }
}
