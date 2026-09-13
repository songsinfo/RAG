package dev.rag.knowledge;

import dev.rag.common.*;

import java.util.*;
import java.util.regex.*;
import java.nio.charset.StandardCharsets;
import java.io.*;
import java.net.URI;
import java.nio.file.Path;
import java.util.zip.*;

import org.jsoup.Jsoup;

/**
 * Wiki 打包与页面解耦解析组件。
 * 负责将包含多篇页面与本地图片资产的综合 Wiki 压缩包，按页面维度安全拆分为独立的单页面归档，
 * 并提供本地引用图片的安全路径解析、格式校验与 Data URI 内联渲染。
 */
public final class WikiPackage {
    /**
     * 单页面归档实体 Record。
     *
     * @param name  子包文件名（如 pageName.wiki.zip）
     * @param bytes 包含单个页面及该页面专属本地图片资产的 ZIP 二进制
     */
    public record Page(String name, byte[] bytes) {
    }

    private static final Pattern IMAGE = Pattern.compile("!\\[[^]]*]\\(([^)\\s]+)(?:\\s+[^)]*)?\\)");

    /**
     * 将包含多页面的 Wiki 压缩包拆解为独立的单页面任务包。
     * <p>
     * <b>拆包流程：</b>
     * 1. 安全解压提取包内所有文件；
     * 2. 筛选 Markdown/HTML 页面文件（限制 1~100 个）；
     * 3. 针对每个页面提取其引用的本地图片，校验图片必须存在于包内且格式合规；
     * 4. 仅打包该页面及其专属依赖图片，生成轻量级单页 .wiki.zip。
     *
     * @param zip 原始 Wiki 归档字节流
     * @return 拆分后的单页面包列表
     * @throws ApiException 若页面数不在 1~100 之间或缺失引用的本地图片资产
     */
    public static List<Page> split(byte[] zip) {
        var files = InputValidation.archive(zip);
        var pages = files.keySet().stream().filter(WikiPackage::page).toList();
        if (pages.isEmpty() || pages.size() > 100)
            throw new ApiException(422, "WIKI_PACKAGE_INVALID", "Wiki包需包含1到100个HTML/Markdown页面");
        List<Page> result = new ArrayList<>();
        for (String name : pages) {
            String text = StructuredParser.utf8(files.get(name));
            List<String> images = references(name, text);
            Map<String, byte[]> selected = new LinkedHashMap<>();
            selected.put(name, files.get(name));
            for (String reference : images) {
                String path = resolve(name, reference);
                byte[] data = files.get(path);
                if (data == null) throw new ApiException(422, "WIKI_ASSET_MISSING", "Wiki页面缺少本地图片");
                mime(data);
                selected.put(path, data);
            }
            try {
                var out = new ByteArrayOutputStream();
                try (var z = new ZipOutputStream(out)) {
                    for (var e : selected.entrySet()) {
                        z.putNextEntry(new ZipEntry(e.getKey()));
                        z.write(e.getValue());
                        z.closeEntry();
                    }
                }
                result.add(new Page(Path.of(name).getFileName().toString() + ".wiki.zip", out.toByteArray()));
            } catch (IOException e) {
                throw new IllegalStateException(e);
            }
        }
        return result;
    }

    /**
     * 解析单页面 Wiki 包，生成包含文本块与内联图片单元的统一结构化结果。
     *
     * @param zip    单页面 .wiki.zip 字节数组
     * @param parser 结构化解析器实例
     * @return 解析单元列表与警告信息包装对象
     * @throws ApiException 若包含多于一个页面、附件丢失或格式损坏
     */
    public static StructuredParser.Result parse(byte[] zip, StructuredParser parser) {
        var files = InputValidation.archive(zip);
        var names = files.keySet().stream().filter(WikiPackage::page).toList();
        if (names.size() != 1) throw new ApiException(422, "WIKI_PACKAGE_INVALID", "内部页面包必须仅含一个页面");
        String page = names.getFirst(), text = StructuredParser.utf8(files.get(page));
        var result = parser.parse(files.get(page), page);
        List<StructuredParser.Unit> units = new ArrayList<>(result.units());
        List<Map<String, Object>> warnings = new ArrayList<>(result.warnings().stream().filter(w -> !w.get("code").equals("IMAGE_ATTACHMENT_REQUIRED")).toList());
        int ordinal = 0;
        for (String ref : references(page, text)) {
            String path = resolve(page, ref);
            byte[] image = files.get(path);
            if (image == null) throw new ApiException(422, "WIKI_ASSET_MISSING", "Wiki附件不存在");
            String mime = mime(image);
            units.add(new StructuredParser.Unit("image:" + ordinal++, "IMAGE", "", Map.of("wikiPage", page, "localAsset", path, "sourceReference", ref), true, "data:" + mime + ";base64," + Base64.getEncoder().encodeToString(image)));
        }
        return new StructuredParser.Result(units, warnings);
    }

    /**
     * 从 Markdown 或 HTML 页面文本中提取所有引用的本地图片路径。
     * 单页面限制最多 50 张图片。
     *
     * @param page 页面文件名
     * @param text 页面文本内容
     * @return 去重后的图片路径列表
     * @throws ApiException 若图片引用数超过 50（413 TOO_MANY_IMAGES）
     */
    private static List<String> references(String page, String text) {
        List<String> result = new ArrayList<>();
        if (page.toLowerCase(Locale.ROOT).endsWith(".html") || page.toLowerCase(Locale.ROOT).endsWith(".htm")) {
            for (var image : Jsoup.parse(text).select("img[src]")) result.add(image.attr("src"));
        } else {
            var matcher = IMAGE.matcher(text);
            while (matcher.find()) result.add(matcher.group(1));
        }
        if (result.size() > 50) throw new ApiException(413, "TOO_MANY_IMAGES", "单页面最多50张图片");
        return result.stream().distinct().toList();
    }

    /**
     * 基于页面自身路径相对解析图片路径，并执行防逃逸越界检查。
     * 阻止包含协议、远程 URL、查询参数或 ".." 越界路径。
     *
     * @param page      页面相对路径
     * @param reference 图片引用原始字符串
     * @return 规范化后的 ZIP 包内路径
     * @throws ApiException 若路径存在越界或包含外部 URL（422 WIKI_ASSET_UNSAFE）
     */
    private static String resolve(String page, String reference) {
        try {
            URI uri = URI.create(reference);
            if (uri.isAbsolute() || uri.getRawAuthority() != null || uri.getQuery() != null || uri.getFragment() != null || reference.contains("\\") || reference.contains(":"))
                throw unsafe();
            String decoded = uri.getPath();
            Path relative = Path.of(decoded);
            if (relative.isAbsolute()) throw unsafe();
            Path parent = Path.of(page).getParent();
            Path path = (parent == null ? relative : parent.resolve(relative)).normalize();
            if (path.startsWith("..")) throw unsafe();
            return path.toString().replace('\\', '/');
        } catch (IllegalArgumentException e) {
            throw unsafe();
        }
    }

    /**
     * 检验图片二进制头部 Magic Bytes 并返回 MIME 类型。
     * 限制单张图片最大 10 MiB，仅支持 PNG 与 JPEG。
     *
     * @param b 图片文件字节流
     * @return 匹配的 MIME 类型字符串（image/png 或 image/jpeg）
     * @throws ApiException 若图片体积超标（413）或格式非 PNG/JPEG（422）
     */
    public static String mime(byte[] b) {
        if (b.length > 10 * 1024 * 1024) throw new ApiException(413, "IMAGE_TOO_LARGE", "图片超过10MiB");
        if (b.length > 8 && b[0] == (byte) 0x89 && b[1] == 'P' && b[2] == 'N' && b[3] == 'G') return "image/png";
        if (b.length > 3 && b[0] == (byte) 0xff && b[1] == (byte) 0xd8 && b[2] == (byte) 0xff) return "image/jpeg";
        throw new ApiException(422, "IMAGE_UNSUPPORTED", "仅支持本地PNG/JPEG图像");
    }

    /**
     * 判断文件名是否属于支持的 Wiki 页面扩展名（.md, .markdown, .html, .htm）。
     *
     * @param s 文件名
     * @return true 表示为页面文件
     */
    private static boolean page(String s) {
        String n = s.toLowerCase(Locale.ROOT);
        return n.endsWith(".md") || n.endsWith(".markdown") || n.endsWith(".html") || n.endsWith(".htm");
    }

    /**
     * 构造 422 WIKI_ASSET_UNSAFE 路径不安全异常。
     *
     * @return ApiException 实例
     */
    private static ApiException unsafe() {
        return new ApiException(422, "WIKI_ASSET_UNSAFE", "Wiki图片路径越界或包含远程地址");
    }

    private WikiPackage() {
    }
}
