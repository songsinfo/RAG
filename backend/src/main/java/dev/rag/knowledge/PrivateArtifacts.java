package dev.rag.knowledge;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Value;

import java.nio.file.*;
import java.io.*;
import java.util.*;

/**
 * 私有文件工件存储与本地导入路径管理组件。
 * 负责租户隔离的文件持久化、读取、清理，并对本地文件系统导入实施严密的路径穿越与符号链接安全校验。
 */
@Component
public class PrivateArtifacts {
    private final Path root, imports;

    /**
     * 构造 PrivateArtifacts 实例并确保存储根目录存在。
     *
     * @param root    私有文件持久化根目录
     * @param imports 本地导入来源根目录
     * @throws IllegalStateException 若根目录无法创建
     */
    public PrivateArtifacts(@Value("${rag.artifact-root}") String root, @Value("${rag.import-root}") String imports) {
        this.root = Path.of(root).toAbsolutePath().normalize();
        this.imports = Path.of(imports).toAbsolutePath().normalize();
        try {
            Files.createDirectories(this.root);
        } catch (IOException e) {
            throw new IllegalStateException("Private storage unavailable", e);
        }
    }

    /**
     * 持久化租户上传的原始文档字节流。
     * 按照 {tenantId}/{uuid} 的层级结构存储，避免单一目录文件过多。
     *
     * @param tenant 所属租户 ID
     * @param bytes  文档原始字节
     * @return 文件相对存储 Key（格式如 tenantId/uuid）
     * @throws ApiException 若磁盘 IO 写入失败（503 STORAGE_UNAVAILABLE）
     */
    public String save(String tenant, byte[] bytes) {
        String key = tenant + "/" + Json.id();
        try {
            Path p = resolve(key);
            Files.createDirectories(p.getParent());
            Files.write(p, bytes, StandardOpenOption.CREATE_NEW);
            return key;
        } catch (IOException e) {
            throw new ApiException(503, "STORAGE_UNAVAILABLE", "文件存储不可用");
        }
    }

    /**
     * 将相对存储 Key 解析为规范绝对路径，并严格校验防路径穿越。
     *
     * @param key 存储 Key
     * @return 经过安全校验的规范绝对路径
     * @throws IllegalArgumentException 若解析路径超出存储根目录范围
     */
    private Path resolve(String key) {
        Path p = root.resolve(key).normalize();
        if (!p.startsWith(root) || p.equals(root)) throw new IllegalArgumentException("Unsafe storage key");
        return p;
    }

    /**
     * 读取指定存储 Key 的完整文件字节数组。
     *
     * @param key 存储 Key
     * @return 文件二进制字节
     * @throws ApiException 若读取发生 IO 异常（503 ARTIFACT_UNAVAILABLE）
     */
    public byte[] read(String key) {
        try {
            return Files.readAllBytes(resolve(key));
        } catch (IOException e) {
            throw new ApiException(503, "ARTIFACT_UNAVAILABLE", "来源文件不可用");
        }
    }

    /**
     * 删除指定存储 Key 对应的磁盘文件。
     *
     * @param key 存储 Key
     * @throws ApiException 若删除时发生 IO 异常（503 STORAGE_UNAVAILABLE）
     */
    public void delete(String key) {
        try {
            Files.deleteIfExists(resolve(key));
        } catch (IOException e) {
            throw new ApiException(503, "STORAGE_UNAVAILABLE", "文件删除失败");
        }
    }

    /**
     * 安全解析并校验本地导入相对路径。
     * <p>
     * <b>严格安全防护策略：</b>
     * 1. 语法检查：禁止绝对路径、冒号、反斜杠及 ".." 导航符；
     * 2. 根目录限定：规范路径必须严格处于 imports 目录之下且文件必须实际存在；
     * 3. 符号链接穿透防护：逐级向上检查父目录，禁止任何符号链接（SymbolicLink）或特殊设备文件；
     * 4. 真实物理路径校验：验证 toRealPath() 仍然严格处于 imports 物理根目录下，杜绝各种软链接越界攻击。
     *
     * @param relative 相对导入子路径
     * @return 经过安全校验的规范 Path 实例
     * @throws ApiException 若路径非法、越界或存在符号链接风险（400 IMPORT_PATH_INVALID）
     */
    public Path importPath(String relative) {
        try {
            Path r = Path.of(relative);
            if (r.isAbsolute() || relative.contains(":") || relative.contains("\\") || r.normalize().startsWith(".."))
                throw invalid();
            Path p = imports.resolve(r).normalize();
            if (!p.startsWith(imports) || !Files.exists(p, LinkOption.NOFOLLOW_LINKS)) throw invalid();
            for (Path q = p; q != null && q.startsWith(imports); q = q.getParent()) {
                if (Files.isSymbolicLink(q) || Files.readAttributes(q, java.nio.file.attribute.BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS).isOther())
                    throw invalid();
            }
            if (!p.toRealPath().startsWith(imports.toRealPath())) throw invalid();
            return p;
        } catch (IOException | InvalidPathException e) {
            throw invalid();
        }
    }

    /**
     * 预览本地导入目录或文件，返回待导入文件清单。
     * 限制单次最多扫描 100 个文件，单文件最大不超过 25 MiB。
     *
     * @param relative 相对导入路径
     * @return 包含 relativePath, sizeBytes, sha256 的文件信息列表
     * @throws ApiException 若文件数超过 100、单文件超 25MiB 或路径非法
     */
    public List<Map<String, Object>> preview(String relative) {
        Path p = importPath(relative);
        try (var walk = Files.walk(p)) {
            var files = walk.filter(x -> Files.isRegularFile(x, LinkOption.NOFOLLOW_LINKS)).limit(101).toList();
            if (files.size() > 100) throw new ApiException(413, "INPUT_TOO_LARGE", "单次最多 100 个文件");
            List<Map<String, Object>> list = new ArrayList<>();
            for (Path f : files) {
                String rel = imports.relativize(f).toString().replace('\\', '/');
                importPath(rel);
                long size = Files.size(f);
                if (size > 25 * 1024 * 1024) throw new ApiException(413, "INPUT_TOO_LARGE", "单文件超过 25 MiB");
                list.add(Map.of("relativePath", rel, "sizeBytes", size, "sha256", HexFormat.of().formatHex(AccessPolicy.hash(Files.readAllBytes(f)))));
            }
            return list;
        } catch (IOException e) {
            throw invalid();
        }
    }

    /**
     * 构造 400 IMPORT_PATH_INVALID 导入路径非法异常。
     *
     * @return ApiException 实例
     */
    private ApiException invalid() {
        return new ApiException(400, "IMPORT_PATH_INVALID", "导入路径无效或越界");
    }
}
