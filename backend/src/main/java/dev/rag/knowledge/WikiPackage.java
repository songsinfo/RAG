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

/** A page package contains only that page and its referenced local raster images. */
public final class WikiPackage {
 public record Page(String name,byte[] bytes){}
 private static final Pattern IMAGE=Pattern.compile("!\\[[^]]*]\\(([^)\\s]+)(?:\\s+[^)]*)?\\)");
 public static List<Page> split(byte[] zip){var files=InputValidation.archive(zip);var pages=files.keySet().stream().filter(WikiPackage::page).toList();if(pages.isEmpty()||pages.size()>100)throw new ApiException(422,"WIKI_PACKAGE_INVALID","Wiki包需包含1到100个HTML/Markdown页面");List<Page> result=new ArrayList<>();for(String name:pages){String text=StructuredParser.utf8(files.get(name));List<String> images=references(name,text);Map<String,byte[]> selected=new LinkedHashMap<>();selected.put(name,files.get(name));for(String reference:images){String path=resolve(name,reference);byte[] data=files.get(path);if(data==null)throw new ApiException(422,"WIKI_ASSET_MISSING","Wiki页面缺少本地图片");mime(data);selected.put(path,data);}try{var out=new ByteArrayOutputStream();try(var z=new ZipOutputStream(out)){for(var e:selected.entrySet()){z.putNextEntry(new ZipEntry(e.getKey()));z.write(e.getValue());z.closeEntry();}}result.add(new Page(Path.of(name).getFileName().toString()+".wiki.zip",out.toByteArray()));}catch(IOException e){throw new IllegalStateException(e);}}return result;}
 public static StructuredParser.Result parse(byte[] zip,StructuredParser parser){var files=InputValidation.archive(zip);var names=files.keySet().stream().filter(WikiPackage::page).toList();if(names.size()!=1)throw new ApiException(422,"WIKI_PACKAGE_INVALID","内部页面包必须仅含一个页面");String page=names.getFirst(),text=StructuredParser.utf8(files.get(page));var result=parser.parse(files.get(page),page);List<StructuredParser.Unit> units=new ArrayList<>(result.units());List<Map<String,Object>> warnings=new ArrayList<>(result.warnings().stream().filter(w->!w.get("code").equals("IMAGE_ATTACHMENT_REQUIRED")).toList());int ordinal=0;for(String ref:references(page,text)){String path=resolve(page,ref);byte[] image=files.get(path);if(image==null)throw new ApiException(422,"WIKI_ASSET_MISSING","Wiki附件不存在");String mime=mime(image);units.add(new StructuredParser.Unit("image:"+ordinal++,"IMAGE","",Map.of("wikiPage",page,"localAsset",path,"sourceReference",ref),true,"data:"+mime+";base64,"+Base64.getEncoder().encodeToString(image)));}return new StructuredParser.Result(units,warnings);}
 private static List<String> references(String page,String text){List<String> result=new ArrayList<>();if(page.toLowerCase(Locale.ROOT).endsWith(".html")||page.toLowerCase(Locale.ROOT).endsWith(".htm")){for(var image:Jsoup.parse(text).select("img[src]"))result.add(image.attr("src"));}else{var matcher=IMAGE.matcher(text);while(matcher.find())result.add(matcher.group(1));}if(result.size()>50)throw new ApiException(413,"TOO_MANY_IMAGES","单页面最多50张图片");return result.stream().distinct().toList();}
 private static String resolve(String page,String reference){try{URI uri=URI.create(reference);if(uri.isAbsolute()||uri.getRawAuthority()!=null||uri.getQuery()!=null||uri.getFragment()!=null||reference.contains("\\")||reference.contains(":"))throw unsafe();String decoded=uri.getPath();Path relative=Path.of(decoded);if(relative.isAbsolute())throw unsafe();Path parent=Path.of(page).getParent();Path path=(parent==null?relative:parent.resolve(relative)).normalize();if(path.startsWith(".."))throw unsafe();return path.toString().replace('\\','/');}catch(IllegalArgumentException e){throw unsafe();}}
 public static String mime(byte[] b){if(b.length>10*1024*1024)throw new ApiException(413,"IMAGE_TOO_LARGE","图片超过10MiB");if(b.length>8&&b[0]==(byte)0x89&&b[1]=='P'&&b[2]=='N'&&b[3]=='G')return "image/png";if(b.length>3&&b[0]==(byte)0xff&&b[1]==(byte)0xd8&&b[2]==(byte)0xff)return "image/jpeg";throw new ApiException(422,"IMAGE_UNSUPPORTED","仅支持本地PNG/JPEG图像");}
 private static boolean page(String s){String n=s.toLowerCase(Locale.ROOT);return n.endsWith(".md")||n.endsWith(".markdown")||n.endsWith(".html")||n.endsWith(".htm");}
 private static ApiException unsafe(){return new ApiException(422,"WIKI_ASSET_UNSAFE","Wiki图片路径越界或包含远程地址");}
 private WikiPackage(){}
}
