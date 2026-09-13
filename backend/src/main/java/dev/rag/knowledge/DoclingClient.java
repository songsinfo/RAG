package dev.rag.knowledge;
import dev.rag.common.*;
import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Value;
import java.net.*;
import java.net.http.*;
import java.time.Duration;
import java.nio.charset.StandardCharsets;
import java.io.*;
import java.util.*;

@Component
public class DoclingClient {
 private static final String RESERVED_FALLBACK="UNSTRUCTURED";
 private final HttpClient client=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();private final String url,key;
 public DoclingClient(@Value("${rag.docling-url}")String url,@Value("${DOCLING_API_KEY:}")String key){this.url=url;this.key=key;}
 public String submit(byte[] file,String extension){String boundary="rag"+Json.id();try{var out=new ByteArrayOutputStream();for(var e:Map.of("to_formats","json","image_export_mode","embedded","do_ocr","true","do_table_structure","true","include_images","true","do_picture_description","false","abort_on_error","false","document_timeout","180").entrySet()){out.write(("--"+boundary+"\r\nContent-Disposition: form-data; name=\""+e.getKey()+"\"\r\n\r\n"+e.getValue()+"\r\n").getBytes(StandardCharsets.UTF_8));}out.write(("--"+boundary+"\r\nContent-Disposition: form-data; name=\"files\"; filename=\"document."+extension+"\"\r\nContent-Type: application/octet-stream\r\n\r\n").getBytes(StandardCharsets.UTF_8));out.write(file);out.write(("\r\n--"+boundary+"--\r\n").getBytes(StandardCharsets.UTF_8));return request("/v1/convert/file/async","POST",out.toByteArray(),"multipart/form-data; boundary="+boundary).path("task_id").asText();}catch(IOException e){throw unavailable();}}
 public String status(String task){return request("/v1/status/poll/"+safe(task),"GET",null,null).path("task_status").asText();}
 public JsonNode result(String task){return request("/v1/result/"+safe(task),"GET",null,null);}
 private String safe(String s){if(!s.matches("[A-Za-z0-9_-]{1,128}"))throw new ApiException(502,"PARSER_CONTRACT_INVALID","解析服务返回无效任务");return s;}
 private JsonNode request(String path,String method,byte[] bytes,String contentType){try{var b=HttpRequest.newBuilder(URI.create(url+path)).timeout(Duration.ofSeconds(30));if(!key.isBlank())b.header("X-Api-Key",key);if(bytes!=null)b.header("Content-Type",contentType).method(method,HttpRequest.BodyPublishers.ofByteArray(bytes));else b.GET();var r=client.send(b.build(),HttpResponse.BodyHandlers.ofByteArray());if(r.statusCode()==404)throw new ApiException(404,"PARSER_TASK_LOST","解析任务已失效");if(r.statusCode()<200||r.statusCode()>=300)throw unavailable();if(r.body().length>128*1024*1024)throw new ApiException(413,"PARSE_OUTPUT_TOO_LARGE","解析结果超过上限");return Json.MAPPER.readTree(r.body());}catch(InterruptedException e){Thread.currentThread().interrupt();throw unavailable();}catch(IOException e){throw unavailable();}}
 public StructuredParser.Result normalize(JsonNode response,boolean pdf){String status=response.path("status").asText();if(!Set.of("success","partial_success").contains(status))throw new ApiException(422,"PARSE_FAILED","解析未完成");JsonNode doc=response.path("document").path("json_content");if(!doc.isObject())throw new ApiException(502,"PARSER_CONTRACT_INVALID","缺少结构化文档");List<StructuredParser.Unit> units=new ArrayList<>();List<Map<String,Object>> warnings=new ArrayList<>();if(!status.equals("success")||response.path("errors").size()>0)warnings.add(Map.of("code","PARSER_PARTIAL","errorCount",response.path("errors").size()));walk(doc,doc.path("body"),units,warnings,pdf,new HashSet<>(),0);if(units.isEmpty())throw new ApiException(422,"EMPTY_DOCUMENT","解析没有可读单元");if(units.stream().filter(u->u.kind().equals("IMAGE")).count()>50)throw new ApiException(413,"TOO_MANY_IMAGES","文档包含超过 50 张图片");return new StructuredParser.Result(units,warnings);}
 private void walk(JsonNode doc,JsonNode node,List<StructuredParser.Unit> units,List<Map<String,Object>> warnings,boolean pdf,Set<String> visited,int depth){if(depth>100)throw new ApiException(422,"PARSER_STRUCTURE_INVALID","文档结构超限");String ref=node.path("$ref").asText();if(!ref.isBlank()){if(!ref.startsWith("#/")||!visited.add(ref))return;node=doc.at(ref.substring(1));}String self=node.path("self_ref").asText("unit:"+units.size()),label=node.path("label").asText();Map<String,Object> location=new LinkedHashMap<>();location.put("doclingRef",self);if(pdf&&node.path("prov").isArray())location.put("provenance",Json.MAPPER.convertValue(node.path("prov"),List.class));else location.put("paragraph",units.size()+1);String text=node.path("text").asText(),image=null,kind="TEXT";boolean machine=pdf;
  if(label.equals("table")){kind="TABLE";JsonNode cells=node.path("data").path("table_cells");location.put("table",Json.MAPPER.convertValue(node.path("data"),Map.class));StringBuilder table=new StringBuilder();for(JsonNode c:cells)table.append("[r").append(c.path("start_row_offset_idx").asInt()).append("c").append(c.path("start_col_offset_idx").asInt()).append("] ").append(c.path("text").asText()).append('\n');text=table.toString();if(cells.isEmpty())warnings.add(Map.of("code","TABLE_STRUCTURE_MISSING","unit",self));}
  else if(label.equals("picture")){kind="IMAGE";image=node.path("image").path("uri").asText();text="";machine=true;if(!image.startsWith("data:image/png;base64,")&&!image.startsWith("data:image/jpeg;base64,")){warnings.add(Map.of("code","IMAGE_DATA_MISSING","unit",self));image=null;}}
  else if(label.equals("section_header")||label.equals("title"))kind="HEADING";else if(label.equals("code"))kind="CODE";
  if(!text.isBlank()||kind.equals("IMAGE"))units.add(new StructuredParser.Unit(self,kind,text,location,machine,image));for(JsonNode child:node.path("children"))walk(doc,child,units,warnings,pdf,visited,depth+1);
 }
 private ApiException unavailable(){return new ApiException(503,"PARSER_UNAVAILABLE","Docling 解析服务不可用；已预留 "+RESERVED_FALLBACK+" 降级点但当前未配置");}
}
