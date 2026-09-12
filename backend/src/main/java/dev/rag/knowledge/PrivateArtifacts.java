package dev.rag.knowledge;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Value;
import java.nio.file.*;
import java.io.*;
import java.util.*;

@Component
public class PrivateArtifacts {
 private final Path root,imports;
 public PrivateArtifacts(@Value("${rag.artifact-root}")String root,@Value("${rag.import-root}")String imports){this.root=Path.of(root).toAbsolutePath().normalize();this.imports=Path.of(imports).toAbsolutePath().normalize();try{Files.createDirectories(this.root);}catch(IOException e){throw new IllegalStateException("Private storage unavailable",e);}}
 public String save(String tenant,byte[] bytes){String key=tenant+"/"+Json.id();try{Path p=resolve(key);Files.createDirectories(p.getParent());Files.write(p,bytes,StandardOpenOption.CREATE_NEW);return key;}catch(IOException e){throw new ApiException(503,"STORAGE_UNAVAILABLE","文件存储不可用");}}
 private Path resolve(String key){Path p=root.resolve(key).normalize();if(!p.startsWith(root)||p.equals(root))throw new IllegalArgumentException("Unsafe storage key");return p;}
 public byte[] read(String key){try{return Files.readAllBytes(resolve(key));}catch(IOException e){throw new ApiException(503,"ARTIFACT_UNAVAILABLE","来源文件不可用");}}
 public void delete(String key){try{Files.deleteIfExists(resolve(key));}catch(IOException e){throw new ApiException(503,"STORAGE_UNAVAILABLE","文件删除失败");}}
 public Path importPath(String relative){try{Path r=Path.of(relative);if(r.isAbsolute()||relative.contains(":")||relative.contains("\\")||r.normalize().startsWith(".."))throw invalid();Path p=imports.resolve(r).normalize();if(!p.startsWith(imports)||!Files.exists(p,LinkOption.NOFOLLOW_LINKS))throw invalid();for(Path q=p;q!=null&&q.startsWith(imports);q=q.getParent()){if(Files.isSymbolicLink(q)||Files.readAttributes(q,java.nio.file.attribute.BasicFileAttributes.class,LinkOption.NOFOLLOW_LINKS).isOther())throw invalid();}if(!p.toRealPath().startsWith(imports.toRealPath()))throw invalid();return p;}catch(IOException|InvalidPathException e){throw invalid();}}
 public List<Map<String,Object>> preview(String relative){Path p=importPath(relative);try(var walk=Files.walk(p)){var files=walk.filter(x->Files.isRegularFile(x,LinkOption.NOFOLLOW_LINKS)).limit(101).toList();if(files.size()>100)throw new ApiException(413,"INPUT_TOO_LARGE","单次最多 100 个文件");List<Map<String,Object>> list=new ArrayList<>();for(Path f:files){String rel=imports.relativize(f).toString().replace('\\','/');importPath(rel);long size=Files.size(f);if(size>25*1024*1024)throw new ApiException(413,"INPUT_TOO_LARGE","单文件超过 25 MiB");list.add(Map.of("relativePath",rel,"sizeBytes",size,"sha256",HexFormat.of().formatHex(AccessPolicy.hash(Files.readAllBytes(f)))));}return list;}catch(IOException e){throw invalid();}}
 private ApiException invalid(){return new ApiException(400,"IMPORT_PATH_INVALID","导入路径无效或越界");}
}
