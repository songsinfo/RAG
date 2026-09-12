package dev.rag.knowledge;
import dev.rag.common.*;
import org.apache.pdfbox.Loader;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.zip.*;

public final class InputValidation {
 public static String validate(byte[] bytes,String name){if(bytes.length==0)throw new ApiException(422,"EMPTY_DOCUMENT","文件为空");if(bytes.length>25*1024*1024)throw new ApiException(413,"INPUT_TOO_LARGE","单文件最多 25 MiB");String ext=name.substring(name.lastIndexOf('.')+1).toLowerCase(Locale.ROOT);if(!Set.of("md","markdown","txt","jsonl","pdf","docx","html","htm").contains(ext))throw new ApiException(422,"PARSE_UNSUPPORTED","不支持该文件格式");if(ext.equals("pdf")){if(!new String(bytes,0,Math.min(5,bytes.length),StandardCharsets.US_ASCII).equals("%PDF-"))throw invalid();try(var pdf=Loader.loadPDF(bytes)){if(pdf.isEncrypted()||pdf.getNumberOfPages()>200)throw new ApiException(422,"PDF_LIMIT","PDF 加密或超过 200 页");}catch(IOException e){throw invalid();}}else if(ext.equals("docx")){var files=archive(bytes);if(!files.containsKey("word/document.xml"))throw invalid();for(var e:files.entrySet())if(e.getKey().endsWith(".rels")&&new String(e.getValue(),StandardCharsets.UTF_8).matches("(?s).*TargetMode\\s*=\\s*[\"']External[\"'].*"))throw new ApiException(422,"REMOTE_RESOURCE_BLOCKED","DOCX 包含外部关系，请导出本地副本");}else StructuredParser.utf8(bytes);return ext;}
 public static Map<String,byte[]> archive(byte[] bytes){Map<String,byte[]> files=new LinkedHashMap<>();long total=0;try(var zip=new ZipInputStream(new ByteArrayInputStream(bytes))){ZipEntry e;int count=0;while((e=zip.getNextEntry())!=null){if(++count>1000)throw invalid();String name=e.getName();if(name.startsWith("/")||name.contains("\\")||name.contains(":")||Arrays.asList(name.split("/")).contains("..")||name.toLowerCase(Locale.ROOT).endsWith(".zip"))throw invalid();if(e.isDirectory())continue;byte[] data=zip.readNBytes(25*1024*1024+1);if(data.length>25*1024*1024|| (total+=data.length)>250L*1024*1024||total>Math.max(1,bytes.length)*100L)throw new ApiException(413,"ARCHIVE_LIMIT","压缩包超过安全限制");if(files.put(name,data)!=null)throw invalid();}return files;}catch(IOException e){throw invalid();}}
 private static ApiException invalid(){return new ApiException(422,"FILE_INVALID","文件内容或容器结构无效");}
 private InputValidation(){}
}
