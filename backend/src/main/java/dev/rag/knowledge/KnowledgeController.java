package dev.rag.knowledge;

import dev.rag.common.*;
import dev.rag.identity.*;
import jakarta.servlet.http.*;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.http.*;

import java.nio.file.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 知识库文档摄取、生命周期、后台作业及离线导入 REST 控制器。
 * <p>
 * 提供普通用户文档浏览、管理员文档与版本上传（支持单文件、批量与 Wiki 离线包）、构件二进制安全下载、
 * 生命周期流转、后台异步作业跟踪与干预、导入目录预估及执行等 HTTP 端点。
 */
@RestController
@RequestMapping("/api/v1")
public class KnowledgeController {
    private final KnowledgeService service;
    private final AccessPolicy access;
    private final PrivateArtifacts files;
    private final Db db;

    /**
     * 本地导入目录预估会话上下文缓存。
     *
     * @param tenant 租户标识
     * @param user 创建预估的用户标识
     * @param expires 预估凭证过期时间戳（毫秒）
     * @param files 预估发现的文件元数据列表（含相对路径与哈希）
     */
    private record Preview(String tenant, String user, long expires, List<Map<String, Object>> files) {
    }

    private final Map<String, Preview> previews = new ConcurrentHashMap<>();

    /**
     * 构造知识库 Web 控制器。
     *
     * @param service 知识库业务服务
     * @param access 访问控制与主体提取组件
     * @param files 私有文件与本地导入管理组件
     * @param db 统一数据库操作组件
     */
    public KnowledgeController(KnowledgeService service, AccessPolicy access, PrivateArtifacts files, Db db) {
        this.service = service;
        this.access = access;
        this.files = files;
        this.db = db;
    }

    /**
     * 普通用户查询可见文档列表。
     *
     * @param r HTTP 请求对象（用于提取上下文主体 Actor）
     * @param kbId 可选的知识库 ID 过滤条件
     * @return 过滤并脱敏后的文档列表
     */
    @GetMapping("/documents")
    public Object list(HttpServletRequest r, @RequestParam(required = false) String kbId) {
        return service.list(access.actor(r), kbId, false, null);
    }

    /**
     * 管理员查询租户全部文档列表。
     *
     * @param r HTTP 请求对象
     * @param kbId 可选的知识库 ID 过滤条件
     * @param status 文档状态过滤条件，默认为 "LIVE"
     * @return 匹配状态的文档列表
     */
    @GetMapping("/admin/documents")
    public Object adminList(HttpServletRequest r, @RequestParam(required = false) String kbId, @RequestParam(defaultValue = "LIVE") String status) {
        return service.list(access.actor(r), kbId, true, status);
    }

    /**
     * 查询指定文档的基础详情元数据。
     *
     * @param r HTTP 请求对象
     * @param id 文档唯一标识 ID
     * @return 文档元数据 Map
     */
    @GetMapping("/documents/{id}")
    public Object get(HttpServletRequest r, @PathVariable String id) {
        return service.get(access.actor(r), id);
    }

    /**
     * 上传新文档文件并异步排队索引摄取任务。
     *
     * @param r HTTP 请求对象
     * @param file 待上传的文件表单项（Multipart）
     * @param kbId 归属知识库 ID
     * @param buildConfig 索引构建定制 JSON 配置，默认为 "{}"
     * @param key 客户端幂等唯一请求头（Idempotency-Key）
     * @return 包含文档 ID 与排队任务 ID 的响应
     * @throws IOException 若读取文件输入流失败
     */
    @PostMapping(value = "/admin/documents", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Object upload(HttpServletRequest r, @RequestParam("file") MultipartFile file, @RequestParam String kbId, @RequestParam(defaultValue = "{}") String buildConfig, @RequestHeader("Idempotency-Key") String key) throws IOException {
        return service.upload(access.actor(r), kbId, null, name(file), file.getBytes(), key, Json.object(buildConfig));
    }

    /**
     * 批量上传多份文档（单次最多 100 个文件）。
     * <p>
     * 为每个文件派生子幂等键（key-0, key-1, ...），捕获单个文件的 ApiException 错误并返回独立状态，不中断整批处理。
     *
     * @param r HTTP 请求对象
     * @param input 多文件表单列表
     * @param kbId 归属知识库 ID
     * @param key 批量请求主幂等键
     * @return 每个文件的上传/入队结果列表
     * @throws IOException 若文件流读取异常
     */
    @PostMapping(value = "/admin/documents/batch", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Object batch(HttpServletRequest r, @RequestParam("files") List<MultipartFile> input, @RequestParam String kbId, @RequestHeader("Idempotency-Key") String key) throws IOException {
        Actor a = access.actor(r);
        access.requireAdmin(a);
        if (input.size() > 100) throw new ApiException(413, "INPUT_TOO_LARGE", "最多100个文件");
        List<Object> results = new ArrayList<>();
        int i = 0;
        for (var f : input) {
            try {
                results.add(service.upload(a, kbId, null, name(f), f.getBytes(), key + "-" + i++, Map.of()));
            } catch (ApiException e) {
                results.add(Map.of("fileName", name(f), "status", "FAILED", "errorCode", e.code));
            }
        }
        return results;
    }

    /**
     * 为现有文档追加上传新版本文件。
     *
     * @param r HTTP 请求对象
     * @param id 目标文档唯一标识 ID
     * @param file 新版本文件表单项
     * @param buildConfig 索引构建定制配置
     * @param key 幂等请求头
     * @return 包含新版本与任务作业信息的响应
     * @throws IOException 若读取文件流失败
     */
    @PostMapping(value = "/admin/documents/{id}/versions", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Object version(HttpServletRequest r, @PathVariable String id, @RequestParam("file") MultipartFile file, @RequestParam(defaultValue = "{}") String buildConfig, @RequestHeader("Idempotency-Key") String key) throws IOException {
        Actor a = access.actor(r);
        var d = service.get(a, id);
        return service.upload(a, d.get("kbId").toString(), id, name(file), file.getBytes(), key, Json.object(buildConfig));
    }

    /**
     * 预览指定文档版本的构建产物与解析单元（抽检前 100 个单元）。
     *
     * @param r HTTP 请求对象
     * @param id 文档 ID
     * @param versionId 文档版本 ID
     * @return 解析文本单元与告警信息列表
     */
    @GetMapping("/documents/{id}/versions/{versionId}/preview")
    public Object preview(HttpServletRequest r, @PathVariable String id, @PathVariable String versionId) {
        return service.preview(access.actor(r), id, versionId);
    }

    /**
     * 安全下载指定的构件文件（如源文件、解析中间件或图片产物）。
     * <p>
     * 强制使用 application/octet-stream 响应、配置 nosniff 与 no-store，避免浏览器直接执行或本地不当缓存。
     *
     * @param r HTTP 请求对象
     * @param response HTTP 响应输出对象
     * @param id 文档 ID
     * @param artifactId 构件 ID
     */
    @GetMapping("/documents/{id}/artifacts/{artifactId}")
    public void artifact(HttpServletRequest r, HttpServletResponse response, @PathVariable String id, @PathVariable String artifactId) {
        Actor a = access.actor(r);
        access.guard(a, List.of(id), () -> {
            var artifact = db.one("SELECT storage_key,kind FROM artifact WHERE tenant_id=:t AND document_id=:d AND id=:id AND status='READY'", Db.params("t", a.tenantId(), "d", id, "id", artifactId));
            response.setHeader("Content-Disposition", "attachment; filename=\"source.bin\"");
            response.setHeader("X-Content-Type-Options", "nosniff");
            response.setHeader("Cache-Control", "no-store");
            response.setContentType("application/octet-stream");
            try {
                response.getOutputStream().write(files.read(artifact.get("storage_key").toString()));
                response.flushBuffer();
            } catch (IOException e) {
                throw new ApiException(503, "DOWNLOAD_INTERRUPTED", "下载中断");
            }
            return null;
        });
    }

    /**
     * 文档生命周期流转控制（移入回收站 trash / 恢复 restore / 彻底清除 purge）。
     *
     * @param r HTTP 请求对象
     * @param id 目标文档 ID
     * @param action 生命周期操作动作（trash / restore / purge）
     * @param b 请求体，必须包含 "expectedVersion" 期望行版本号（用于乐观锁并发防覆写）
     * @return 状态流转结果
     */
    @PostMapping("/admin/documents/{id}/{action:trash|restore|purge}")
    public Object lifecycle(HttpServletRequest r, @PathVariable String id, @PathVariable String action, @RequestBody Map<String, Object> b) {
        return service.lifecycle(access.actor(r), id, action, IdentityService.number(b, "expectedVersion"));
    }

    /**
     * 管理员查询租户最近 100 条后台异步任务作业记录。
     *
     * @param r HTTP 请求对象
     * @return 任务作业概要列表
     */
    @GetMapping("/admin/jobs")
    public Object jobs(HttpServletRequest r) {
        return service.jobs(access.actor(r), null);
    }

    /**
     * 管理员查询单个后台作业任务的详细状态。
     *
     * @param r HTTP 请求对象
     * @param id 任务作业 ID
     * @return 任务作业详情
     * @throws ApiException 若任务不存在
     */
    @GetMapping("/admin/jobs/{id}")
    public Object job(HttpServletRequest r, @PathVariable String id) {
        var list = (List<?>) service.jobs(access.actor(r), id);
        if (list.isEmpty()) throw ApiException.missing();
        return list.getFirst();
    }

    /**
     * 控制后台作业状态（重试 retry 或取消 cancel）。
     *
     * @param r HTTP 请求对象
     * @param id 目标作业 ID
     * @param action 控制动作（retry 或 cancel）
     * @return 变更后的作业记录列表
     */
    @PostMapping("/admin/jobs/{id}/{action:retry|cancel}")
    public Object control(HttpServletRequest r, @PathVariable String id, @PathVariable String action) {
        return service.controlJob(access.actor(r), id, action.equals("cancel"));
    }

    /**
     * 人工审核确认构建质量警告，放行进入分块阶段。
     *
     * @param r HTTP 请求对象
     * @param id 处于 NEEDS_REVIEW 状态的 buildId
     * @param b 请求体，必须包含 "reason" 审核确认说明
     * @return 更新后的构建状态
     */
    @PostMapping("/admin/builds/{id}/accept-warnings")
    public Object accept(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> b) {
        return service.accept(access.actor(r), id, b);
    }

    /**
     * 扫描服务器本地导入根目录并生成文件导入预估信息。
     * <p>
     * 限制预估会话有效期为 10 分钟，最多同时保留 100 个有效预估会话。
     *
     * @param r HTTP 请求对象
     * @param b 请求体，包含 "relativePath" 和可选的 "rootAlias"
     * @return 包含 estimateId、扫描文件清单和预估消耗信息的响应 Map
     */
    @PostMapping("/admin/import-previews")
    public Object importPreview(HttpServletRequest r, @RequestBody Map<String, Object> b) {
        Actor a = access.actor(r);
        access.requireAdmin(a);
        String alias = Json.str(b, "rootAlias");
        if (!alias.isBlank() && !alias.equals("default")) throw IdentityService.invalid("rootAlias 无效");
        var listing = files.preview(Json.str(b, "relativePath"));
        previews.entrySet().removeIf(e -> e.getValue().expires() < System.currentTimeMillis());
        if (previews.size() >= 100) throw new ApiException(429, "RATE_LIMITED", "导入预览过多");
        String id = Json.id();
        previews.put(id, new Preview(a.tenantId(), a.userId(), System.currentTimeMillis() + 600000, listing));
        return Map.of("estimateId", id, "files", listing, "estimatedModelCost", 0, "mode", "BM25_ONLY");
    }

    /**
     * 确认并批量执行本地目录文件的离线导入。
     * <p>
     * <b>严格校验：</b>
     * <ul>
     *   <li>校验预估会话有效性与租户/用户归属一致性；</li>
     *   <li>每次最多导入 100 个文件，必须属于预估扫描列表内；</li>
     *   <li>重新读取文件并校验 SHA-256 哈希值，防止预估后文件被并发修改。</li>
     * </ul>
     *
     * @param r HTTP 请求对象
     * @param b 请求体，包含 estimateId、kbId 及选定的 files 文件相对路径列表
     * @return 每个文件的排队摄取结果列表
     */
    @PostMapping("/admin/imports")
    public Object imports(HttpServletRequest r, @RequestBody Map<String, Object> b) {
        Actor a = access.actor(r);
        access.requireAdmin(a);
        String id = Json.str(b, "estimateId");
        var p = previews.get(id);
        if (p == null || p.expires() < System.currentTimeMillis() || !p.tenant().equals(a.tenantId()) || !p.user().equals(a.userId()))
            throw new ApiException(409, "ESTIMATE_EXPIRED", "导入预览已失效");
        Object input = b.get("files");
        if (!(input instanceof List<?> selected) || selected.isEmpty() || selected.size() > 100)
            throw IdentityService.invalid("files 无效");
        List<Object> results = new ArrayList<>();
        for (Object f : selected) {
            String path = f.toString();
            var entry = p.files().stream().filter(e -> e.get("relativePath").equals(path)).findFirst().orElseThrow(() -> IdentityService.invalid("文件不在预览内"));
            try {
                Path actual = files.importPath(path);
                byte[] bytes = Files.readAllBytes(actual);
                if (!entry.get("sha256").equals(HexFormat.of().formatHex(AccessPolicy.hash(bytes))))
                    throw new ApiException(409, "SOURCE_CHANGED", "导入文件已变化");
                results.add(service.upload(a, IdentityService.required(b, "kbId", 36), null, actual.getFileName().toString(), bytes, id + "-" + results.size(), Map.of()));
            } catch (IOException e) {
                throw new ApiException(503, "IMPORT_UNAVAILABLE", "导入文件不可读");
            }
        }
        return results;
    }

    /**
     * 清洗并标准化上传文件名。
     * <p>
     * 去除 Windows/Unix 路径前缀（防御路径穿越），限制最长 512 字符。
     *
     * @param f 上传的 MultipartFile 对象
     * @return 清洗后的安全文件名
     */
    private String name(MultipartFile f) {
        String name = f.getOriginalFilename();
        if (name == null || name.isBlank() || name.length() > 512) throw IdentityService.invalid("文件名无效");
        return name.replace('\\', '/').substring(name.replace('\\', '/').lastIndexOf('/') + 1);
    }

    /**
     * 上传并解包导入 Wiki 知识库归档文件。
     * <p>
     * 限制归档包大小不超过 25MiB，调用 {@link WikiPackage#split} 解析包内页面，并依序排队摄取。
     *
     * @param r HTTP 请求对象
     * @param file Wiki 归档压缩文件
     * @param kbId 归属知识库 ID
     * @param buildConfig 构建配置
     * @param key 批量请求主幂等键
     * @return 各页面的上传与排队结果
     * @throws IOException 若解包或读取文件流失败
     */
    @PostMapping(value = "/admin/wiki-imports", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Object wiki(HttpServletRequest r, @RequestParam("file") MultipartFile file, @RequestParam String kbId, @RequestParam(defaultValue = "{}") String buildConfig, @RequestHeader("Idempotency-Key") String key) throws IOException {
        Actor a = access.actor(r);
        access.requireAdmin(a);
        if (file.getSize() > 25 * 1024 * 1024) throw new ApiException(413, "INPUT_TOO_LARGE", "Wiki包最多25MiB");
        List<Object> results = new ArrayList<>();
        for (var page : WikiPackage.split(file.getBytes()))
            results.add(service.upload(a, kbId, null, page.name(), page.bytes(), key + "-" + results.size(), Json.object(buildConfig)));
        return results;
    }
}
