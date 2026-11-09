// Offscreen replay of the original map SPIR-V, including its original vertex shader.
// No SPIRV-Cross translation, shader rewriting, game process, hooks or game writes.
#define NOMINMAX
#define VK_NO_PROTOTYPES
#include <windows.h>
#include <vulkan/vulkan_core.h>
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
namespace fs=std::filesystem;
#define FUNCTIONS(X) \
 X(vkCreateInstance) X(vkDestroyInstance) X(vkEnumeratePhysicalDevices) \
 X(vkGetPhysicalDeviceProperties) X(vkGetPhysicalDeviceProperties2) X(vkGetPhysicalDeviceFeatures2) \
 X(vkGetPhysicalDeviceFormatProperties) X(vkGetPhysicalDeviceImageFormatProperties) \
 X(vkGetPhysicalDeviceQueueFamilyProperties) X(vkGetPhysicalDeviceMemoryProperties) \
 X(vkCreateDevice) X(vkDestroyDevice) X(vkDeviceWaitIdle) X(vkGetDeviceQueue) \
 X(vkCreateBuffer) X(vkDestroyBuffer) X(vkGetBufferMemoryRequirements) X(vkBindBufferMemory) \
 X(vkAllocateMemory) X(vkFreeMemory) X(vkMapMemory) X(vkUnmapMemory) \
 X(vkCreateImage) X(vkDestroyImage) X(vkGetImageMemoryRequirements) X(vkBindImageMemory) \
 X(vkCreateImageView) X(vkDestroyImageView) X(vkCreateSampler) X(vkDestroySampler) \
 X(vkCreateShaderModule) X(vkDestroyShaderModule) X(vkCreateDescriptorSetLayout) X(vkDestroyDescriptorSetLayout) \
 X(vkCreateDescriptorPool) X(vkDestroyDescriptorPool) X(vkAllocateDescriptorSets) X(vkUpdateDescriptorSets) \
 X(vkCreatePipelineLayout) X(vkDestroyPipelineLayout) X(vkCreateGraphicsPipelines) X(vkDestroyPipeline) \
 X(vkCreateRenderPass) X(vkDestroyRenderPass) X(vkCreateFramebuffer) X(vkDestroyFramebuffer) \
 X(vkCreateCommandPool) X(vkDestroyCommandPool) X(vkAllocateCommandBuffers) \
 X(vkBeginCommandBuffer) X(vkEndCommandBuffer) X(vkQueueSubmit) X(vkQueueWaitIdle) \
 X(vkCmdPipelineBarrier) X(vkCmdCopyBufferToImage) X(vkCmdCopyImageToBuffer) \
 X(vkCmdBeginRenderPass) X(vkCmdEndRenderPass) X(vkCmdBindPipeline) \
 X(vkCmdBindVertexBuffers) X(vkCmdBindDescriptorSets) X(vkCmdDraw)
static void check(VkResult result,const char*where){if(result!=VK_SUCCESS)throw std::runtime_error(std::string(where)+": "+std::to_string(result));}
#define OK(call) check(call,#call)
static std::vector<char> read(const fs::path&path){
 std::ifstream in(path,std::ios::binary|std::ios::ate);if(!in)throw std::runtime_error("Cannot read "+path.string());
 std::vector<char> bytes(size_t(in.tellg()));in.seekg(0);in.read(bytes.data(),bytes.size());
 if(!in)throw std::runtime_error("Truncated "+path.string());return bytes;
}
struct Buffer{VkBuffer handle{};VkDeviceMemory memory{};};
struct Image{VkImage handle{};VkDeviceMemory memory{};VkImageView view{};};
struct Replay{
#define DECLARE(name) PFN_##name name{};
 FUNCTIONS(DECLARE)
#undef DECLARE
 HMODULE loader{};VkInstance instance{};VkPhysicalDevice physical{};VkDevice device{};VkQueue queue{};uint32_t family{};
 VkPhysicalDeviceMemoryProperties memories{};VkCommandPool commandPool{};VkCommandBuffer command{};
 VkDescriptorPool descriptorPool{};VkPipelineLayout pipelineLayout{};VkPipeline pipeline{};
 VkRenderPass renderPass{};VkFramebuffer framebuffer{};
 std::vector<Buffer> buffers;std::vector<Image> images;std::vector<VkSampler> samplers;
 std::vector<VkShaderModule> shaders;std::vector<VkDescriptorSetLayout> layouts;
 static std::vector<std::string> requirements(Replay&r,VkPhysicalDevice candidate,uint32_t pixels,uint32_t&graphicsFamily){
  std::vector<std::string>errors;
  VkPhysicalDeviceDescriptorIndexingFeatures indexing{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES};
  VkPhysicalDeviceFeatures2 features{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2};features.pNext=&indexing;
  r.vkGetPhysicalDeviceFeatures2(candidate,&features);
  VkPhysicalDeviceDescriptorIndexingProperties descriptors{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_PROPERTIES};
  VkPhysicalDeviceProperties2 properties{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};properties.pNext=&descriptors;
  r.vkGetPhysicalDeviceProperties2(candidate,&properties);auto&limits=properties.properties.limits;
  auto need=[&](bool yes,const char*name){if(!yes)errors.emplace_back(name);};
  need(properties.properties.apiVersion>=VK_API_VERSION_1_2,"Vulkan API 1.2");
  need(features.features.textureCompressionBC,"BC texture compression");
  need(features.features.shaderSampledImageArrayDynamicIndexing,"sampled-image array dynamic indexing");
  need(indexing.shaderSampledImageArrayNonUniformIndexing,"sampled-image array non-uniform indexing");
  need(indexing.descriptorBindingSampledImageUpdateAfterBind,"sampled-image update-after-bind descriptors");
  need(limits.maxBoundDescriptorSets>=5,"five bound descriptor sets");
  need(limits.maxDescriptorSetSampledImages>=6&&limits.maxPerStageDescriptorSampledImages>=6,"six ordinary sampled-image descriptors");
  need(limits.maxDescriptorSetSamplers>=3&&limits.maxPerStageDescriptorSamplers>=3,"three sampler descriptors");
  need(limits.maxDescriptorSetStorageBuffers>=4&&limits.maxPerStageDescriptorStorageBuffers>=4,"four storage buffers");
  need(limits.maxPerStageResources>=13,"13 ordinary per-stage resources");
  need(descriptors.maxPerStageDescriptorUpdateAfterBindSampledImages>=32774&&descriptors.maxDescriptorSetUpdateAfterBindSampledImages>=32774,"32774 update-after-bind sampled images");
  need(descriptors.maxPerStageDescriptorUpdateAfterBindSamplers>=3&&descriptors.maxDescriptorSetUpdateAfterBindSamplers>=3,"three update-after-bind samplers");
  need(descriptors.maxPerStageDescriptorUpdateAfterBindStorageBuffers>=4&&descriptors.maxDescriptorSetUpdateAfterBindStorageBuffers>=4,"four update-after-bind storage buffers");
  need(descriptors.maxPerStageUpdateAfterBindResources>=32781&&descriptors.maxUpdateAfterBindDescriptorsInAllPools>=32781,"32781 update-after-bind resources/pool descriptors");
  need(limits.maxImageDimension1D>=151&&limits.maxImageDimension2D>=std::max(1280u,pixels),"map image dimensions");
  need(limits.maxFramebufferWidth>=pixels&&limits.maxFramebufferHeight>=pixels&&limits.maxViewportDimensions[0]>=pixels&&limits.maxViewportDimensions[1]>=pixels,"tile framebuffer and viewport dimensions");
  need(limits.maxStorageBufferRange>=400,"400-byte storage buffer range");
  need(limits.maxMemoryAllocationCount>=421,"421 simultaneous Vulkan memory allocations");
  need(limits.maxVertexInputAttributes>=7&&limits.maxVertexInputBindings>=1&&limits.maxVertexInputBindingStride>=32&&limits.maxVertexInputAttributeOffset>=24,"original vertex layout locations/stride");
  for(auto format:{VK_FORMAT_R8G8B8A8_UNORM,VK_FORMAT_R16_UNORM,VK_FORMAT_BC4_UNORM_BLOCK,VK_FORMAT_BC5_UNORM_BLOCK,VK_FORMAT_BC7_UNORM_BLOCK}){
   VkFormatProperties fp{};r.vkGetPhysicalDeviceFormatProperties(candidate,format,&fp);
   VkFormatFeatureFlags required=VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT|VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT|VK_FORMAT_FEATURE_TRANSFER_DST_BIT;
   if((fp.optimalTilingFeatures&required)!=required)errors.emplace_back("sampled/filter/transfer texture format "+std::to_string(format));
   VkImageFormatProperties ip{};uint32_t extent=format==VK_FORMAT_R16_UNORM?1280:format==VK_FORMAT_BC7_UNORM_BLOCK?256:1024;
   auto result=r.vkGetPhysicalDeviceImageFormatProperties(candidate,format,VK_IMAGE_TYPE_2D,VK_IMAGE_TILING_OPTIMAL,VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT,0,&ip);
   if(result!=VK_SUCCESS||ip.maxExtent.width<extent||ip.maxExtent.height<extent)errors.emplace_back("sampled 2D image support "+std::to_string(format));
  }
  VkImageFormatProperties ip{};
  auto result=r.vkGetPhysicalDeviceImageFormatProperties(candidate,VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_TYPE_1D,VK_IMAGE_TILING_OPTIMAL,VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT,0,&ip);
  need(result==VK_SUCCESS&&ip.maxExtent.width>=151,"sampled RGBA8 1D gradients");
  result=r.vkGetPhysicalDeviceImageFormatProperties(candidate,VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_TYPE_2D,VK_IMAGE_TILING_OPTIMAL,VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT,0,&ip);
  need(result==VK_SUCCESS&&ip.maxExtent.width>=pixels&&ip.maxExtent.height>=pixels,"RGBA8 tile color attachment/readback");
  uint32_t n=0;r.vkGetPhysicalDeviceQueueFamilyProperties(candidate,&n,nullptr);std::vector<VkQueueFamilyProperties>queues(n);r.vkGetPhysicalDeviceQueueFamilyProperties(candidate,&n,queues.data());
  graphicsFamily=UINT32_MAX;for(uint32_t i=0;i<n;++i)if(queues[i].queueCount&&(queues[i].queueFlags&VK_QUEUE_GRAPHICS_BIT)){graphicsFamily=i;break;}
  need(graphicsFamily!=UINT32_MAX,"graphics queue");
  std::cout<<"Candidate: "<<properties.properties.deviceName<<"; API "<<properties.properties.apiVersion<<"; driver "<<properties.properties.driverVersion
   <<"; max update-after-bind images "<<descriptors.maxPerStageDescriptorUpdateAfterBindSampledImages
   <<"; image2D "<<limits.maxImageDimension2D<<"; allocations "<<limits.maxMemoryAllocationCount<<std::endl;
  for(auto&error:errors)std::cout<<"Unsupported: "<<error<<std::endl;
  return errors;
 }
 Replay(uint32_t pixels=2560,bool probe=false){
  loader=LoadLibraryExW(L"vulkan-1.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);if(!loader)throw std::runtime_error("No system Vulkan loader; install a supported graphics driver");
#define LOAD(name) name=reinterpret_cast<PFN_##name>(GetProcAddress(loader,#name));if(!name)throw std::runtime_error("Missing " #name);
  FUNCTIONS(LOAD)
#undef LOAD
  VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};app.pApplicationName="Offline Enshrouded map replay";app.apiVersion=VK_API_VERSION_1_2;
  VkInstanceCreateInfo info{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};info.pApplicationInfo=&app;OK(vkCreateInstance(&info,nullptr,&instance));
  uint32_t count=0;OK(vkEnumeratePhysicalDevices(instance,&count,nullptr));if(!count)throw std::runtime_error("No Vulkan device");
  std::vector<VkPhysicalDevice> devices(count);OK(vkEnumeratePhysicalDevices(instance,&count,devices.data()));
  for(auto candidate:devices){uint32_t graphics=UINT32_MAX;if(requirements(*this,candidate,pixels,graphics).empty()){physical=candidate;family=graphics;break;}}
  if(!physical)throw std::runtime_error("No compatible Vulkan device; see the Unsupported requirements above");
  VkPhysicalDeviceProperties props{};vkGetPhysicalDeviceProperties(physical,&props);
  std::cout<<"Device: "<<props.deviceName<<"; API "<<props.apiVersion<<"; driver "<<props.driverVersion<<std::endl;
  if(probe){std::cout<<"Probe passed; original shader descriptor and texture requirements supported"<<std::endl;return;}
  VkPhysicalDeviceDescriptorIndexingFeatures indexing{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES};
  indexing.shaderSampledImageArrayNonUniformIndexing=VK_TRUE;indexing.descriptorBindingSampledImageUpdateAfterBind=VK_TRUE;
  VkPhysicalDeviceFeatures features{};features.textureCompressionBC=VK_TRUE;features.shaderSampledImageArrayDynamicIndexing=VK_TRUE;
  float priority=1;VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};qi.queueFamilyIndex=family;qi.queueCount=1;qi.pQueuePriorities=&priority;
  VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};di.pNext=&indexing;di.queueCreateInfoCount=1;di.pQueueCreateInfos=&qi;di.pEnabledFeatures=&features;
  OK(vkCreateDevice(physical,&di,nullptr,&device));vkGetDeviceQueue(device,family,0,&queue);vkGetPhysicalDeviceMemoryProperties(physical,&memories);
  VkCommandPoolCreateInfo cp{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};cp.queueFamilyIndex=family;OK(vkCreateCommandPool(device,&cp,nullptr,&commandPool));
  VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};ca.commandPool=commandPool;ca.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ca.commandBufferCount=1;
  OK(vkAllocateCommandBuffers(device,&ca,&command));VkCommandBufferBeginInfo cb{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};OK(vkBeginCommandBuffer(command,&cb));
 }
 ~Replay(){
  if(device){vkDeviceWaitIdle(device);
   if(commandPool)vkDestroyCommandPool(device,commandPool,nullptr);
   if(descriptorPool)vkDestroyDescriptorPool(device,descriptorPool,nullptr);
   if(pipeline)vkDestroyPipeline(device,pipeline,nullptr);
   if(pipelineLayout)vkDestroyPipelineLayout(device,pipelineLayout,nullptr);
   if(framebuffer)vkDestroyFramebuffer(device,framebuffer,nullptr);
   if(renderPass)vkDestroyRenderPass(device,renderPass,nullptr);
   for(auto s:shaders)vkDestroyShaderModule(device,s,nullptr);
   for(auto l:layouts)vkDestroyDescriptorSetLayout(device,l,nullptr);
   for(auto s:samplers)vkDestroySampler(device,s,nullptr);
   for(auto i:images){if(i.view)vkDestroyImageView(device,i.view,nullptr);vkDestroyImage(device,i.handle,nullptr);vkFreeMemory(device,i.memory,nullptr);}
   for(auto b:buffers){vkDestroyBuffer(device,b.handle,nullptr);vkFreeMemory(device,b.memory,nullptr);}
   vkDestroyDevice(device,nullptr);
  }
  if(instance)vkDestroyInstance(instance,nullptr);if(loader)FreeLibrary(loader);
 }
 uint32_t memoryType(uint32_t bits,VkMemoryPropertyFlags flags){
  for(uint32_t i=0;i<memories.memoryTypeCount;++i)if((bits&(1u<<i))&&(memories.memoryTypes[i].propertyFlags&flags)==flags)return i;
  throw std::runtime_error("No matching memory type");
 }
 Buffer buffer(VkDeviceSize size,VkBufferUsageFlags usage,const void*data=nullptr){
  Buffer b;VkBufferCreateInfo ci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};ci.size=size;ci.usage=usage;ci.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
  OK(vkCreateBuffer(device,&ci,nullptr,&b.handle));VkMemoryRequirements req{};vkGetBufferMemoryRequirements(device,b.handle,&req);
  VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};ai.allocationSize=req.size;ai.memoryTypeIndex=memoryType(req.memoryTypeBits,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
  OK(vkAllocateMemory(device,&ai,nullptr,&b.memory));OK(vkBindBufferMemory(device,b.handle,b.memory,0));buffers.push_back(b);
  if(data){void*p=nullptr;OK(vkMapMemory(device,b.memory,0,size,0,&p));memcpy(p,data,size_t(size));vkUnmapMemory(device,b.memory);}return b;
 }
 Image image(uint32_t w,uint32_t h,VkFormat format,bool oneDim,VkImageUsageFlags usage){
  Image image;VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};ci.imageType=oneDim?VK_IMAGE_TYPE_1D:VK_IMAGE_TYPE_2D;ci.format=format;ci.extent={w,h,1};ci.mipLevels=ci.arrayLayers=1;
  ci.samples=VK_SAMPLE_COUNT_1_BIT;ci.tiling=VK_IMAGE_TILING_OPTIMAL;ci.usage=usage;ci.sharingMode=VK_SHARING_MODE_EXCLUSIVE;ci.initialLayout=VK_IMAGE_LAYOUT_UNDEFINED;
  OK(vkCreateImage(device,&ci,nullptr,&image.handle));VkMemoryRequirements req{};vkGetImageMemoryRequirements(device,image.handle,&req);
  VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};ai.allocationSize=req.size;ai.memoryTypeIndex=memoryType(req.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
  OK(vkAllocateMemory(device,&ai,nullptr,&image.memory));OK(vkBindImageMemory(device,image.handle,image.memory,0));
  VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};vi.image=image.handle;vi.viewType=oneDim?VK_IMAGE_VIEW_TYPE_1D:VK_IMAGE_VIEW_TYPE_2D;vi.format=format;vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
  OK(vkCreateImageView(device,&vi,nullptr,&image.view));images.push_back(image);return image;
 }
 void transition(Image image,VkImageLayout before,VkImageLayout after,VkAccessFlags src,VkAccessFlags dst,VkPipelineStageFlags a,VkPipelineStageFlags b){
  VkImageMemoryBarrier barrier{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};barrier.srcAccessMask=src;barrier.dstAccessMask=dst;barrier.oldLayout=before;barrier.newLayout=after;
  barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.image=image.handle;barrier.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
  vkCmdPipelineBarrier(command,a,b,0,0,nullptr,0,nullptr,1,&barrier);
 }
 Image texture(const fs::path&path,uint32_t w,uint32_t h,VkFormat format,bool oneDim){
  auto data=read(path);size_t n=size_t(w)*h*4;
  if(format==VK_FORMAT_R16_UNORM)n=size_t(w)*h*2;
  else if(format==VK_FORMAT_BC4_UNORM_BLOCK)n=size_t((w+3)/4)*((h+3)/4)*8;
  else if(format==VK_FORMAT_BC5_UNORM_BLOCK||format==VK_FORMAT_BC7_UNORM_BLOCK)n=size_t((w+3)/4)*((h+3)/4)*16;
  if(data.size()<n)throw std::runtime_error("Truncated texture payload");
  auto staging=buffer(n,VK_BUFFER_USAGE_TRANSFER_SRC_BIT,data.data());auto result=image(w,h,format,oneDim,VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_SAMPLED_BIT);
  transition(result,VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,0,VK_ACCESS_TRANSFER_WRITE_BIT,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT);
  VkBufferImageCopy copy{};copy.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};copy.imageExtent={w,h,1};
  vkCmdCopyBufferToImage(command,staging.handle,result.handle,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
  transition(result,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
  return result;
 }
 VkSampler sampler(VkSamplerAddressMode address){
  VkSamplerCreateInfo ci{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};ci.magFilter=ci.minFilter=VK_FILTER_LINEAR;ci.mipmapMode=VK_SAMPLER_MIPMAP_MODE_LINEAR;
  ci.addressModeU=ci.addressModeV=ci.addressModeW=address;ci.maxLod=0;
  VkSampler s{};OK(vkCreateSampler(device,&ci,nullptr,&s));samplers.push_back(s);return s;
 }
 VkShaderModule shader(const fs::path&path){
  auto data=read(path);if(data.size()%4)throw std::runtime_error("SPIR-V byte length");
  VkShaderModuleCreateInfo ci{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};ci.codeSize=data.size();ci.pCode=reinterpret_cast<const uint32_t*>(data.data());
  VkShaderModule module{};OK(vkCreateShaderModule(device,&ci,nullptr,&module));shaders.push_back(module);return module;
 }
 VkDescriptorSetLayout layout(std::vector<VkDescriptorSetLayoutBinding> bindings,bool bindless=false){
  std::vector<VkDescriptorBindingFlags>flags(bindings.size(),bindless?VK_DESCRIPTOR_BINDING_UPDATE_AFTER_BIND_BIT:0);
  VkDescriptorSetLayoutBindingFlagsCreateInfo fi{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_BINDING_FLAGS_CREATE_INFO};fi.bindingCount=uint32_t(flags.size());fi.pBindingFlags=flags.data();
  VkDescriptorSetLayoutCreateInfo ci{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};ci.pNext=bindless?&fi:nullptr;ci.flags=bindless?VK_DESCRIPTOR_SET_LAYOUT_CREATE_UPDATE_AFTER_BIND_POOL_BIT:0;ci.bindingCount=uint32_t(bindings.size());ci.pBindings=bindings.data();
  VkDescriptorSetLayout layout{};OK(vkCreateDescriptorSetLayout(device,&ci,nullptr,&layout));layouts.push_back(layout);return layout;
 }
};

int wmain(int argc,wchar_t**argv){try{
 if(argc==3&&std::wstring(argv[1])==L"--probe"){uint32_t pixels=std::stoul(argv[2]);if(pixels!=1280&&pixels!=2560)throw std::runtime_error("Probe supports 1280 or 2560 pixel tiles");Replay r(pixels,true);return 0;}
 if(argc!=9)throw std::runtime_error("Usage: background-vulkan assets output.rgb resolution rasterX rasterY width height profile.bin");
 fs::path root=argv[1],output=argv[2];int resolution=std::stoi(argv[3]);std::array<int,4>rect{};for(int i=0;i<4;++i)rect[i]=std::stoi(argv[4+i]);
 if(resolution<100||resolution>10240||std::min(rect[0],rect[1])<0||std::min(rect[2],rect[3])<=0||int64_t(rect[0])+rect[2]>10240||int64_t(rect[1])+rect[3]>10240||fs::exists(output))throw std::runtime_error("Invalid crop or existing output");
 auto profile=read(argv[8]);if(profile.size()!=32)throw std::runtime_error("Expected 32-byte native profile");
 Replay r(resolution);std::array<Image,207>parts{};
 for(int i=0;i<207;++i){
  uint32_t w=1024,h=1024;VkFormat format=(i%2)?VK_FORMAT_BC4_UNORM_BLOCK:VK_FORMAT_BC5_UNORM_BLOCK;
  if(i<3){w=i==0?151:i==1?128:32;h=1;format=VK_FORMAT_R8G8B8A8_UNORM;}
  else if(i==3){w=h=512;format=VK_FORMAT_BC4_UNORM_BLOCK;}
  else if(i<6){w=h=256;format=VK_FORMAT_BC7_UNORM_BLOCK;}
  else if(i==6){w=h=1280;format=VK_FORMAT_R16_UNORM;}
  parts[i]=r.texture(root/L"assets"/(L"part-"+std::to_wstring(i)+L".bin"),w,h,format,i<3);
 }
 auto clamp=r.sampler(VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE),wrap=r.sampler(VK_SAMPLER_ADDRESS_MODE_REPEAT);
 std::array<float,16>matrix{1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1};
 auto matrixBuffer=r.buffer(64,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,matrix.data());
 auto profileBuffer=r.buffer(32,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,profile.data());
 std::cout<<"Map uniform words:";for(size_t i=0;i<8;++i){uint32_t word{};memcpy(&word,profile.data()+i*4,4);std::cout<<" "<<std::hex<<word;}std::cout<<std::dec<<std::endl;
 std::array<uint32_t,100>heights{},fog{};for(int i=0;i<100;++i){heights[i]=1+i;fog[i]=101+i;}
 auto heightBuffer=r.buffer(400,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,heights.data()),fogBuffer=r.buffer(400,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,fog.data());
 auto binding=[](uint32_t n,VkDescriptorType type,uint32_t count=1){return VkDescriptorSetLayoutBinding{n,type,count,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,nullptr};};
 std::array<VkDescriptorSetLayout,5>layouts{
  r.layout({binding(0,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER),binding(1,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE),binding(2,VK_DESCRIPTOR_TYPE_SAMPLER)}),
  r.layout({binding(0,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER),binding(1,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER),binding(2,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER),binding(3,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE),binding(4,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE),binding(5,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE),binding(6,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE),binding(7,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE),binding(8,VK_DESCRIPTOR_TYPE_SAMPLER),binding(9,VK_DESCRIPTOR_TYPE_SAMPLER)}),
  r.layout({}),r.layout({}),r.layout({binding(0,VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE,32768)},true)};
 std::array<VkDescriptorPoolSize,3>sizes{{{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,4},{VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE,32774},{VK_DESCRIPTOR_TYPE_SAMPLER,3}}};
 VkDescriptorPoolCreateInfo pool{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};pool.flags=VK_DESCRIPTOR_POOL_CREATE_UPDATE_AFTER_BIND_BIT;pool.maxSets=5;pool.poolSizeCount=3;pool.pPoolSizes=sizes.data();OK(r.vkCreateDescriptorPool(r.device,&pool,nullptr,&r.descriptorPool));
 std::array<VkDescriptorSet,5>sets{};VkDescriptorSetAllocateInfo allocate{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};allocate.descriptorPool=r.descriptorPool;allocate.descriptorSetCount=5;allocate.pSetLayouts=layouts.data();OK(r.vkAllocateDescriptorSets(r.device,&allocate,sets.data()));
 auto writeBuffer=[&](int set,int slot,Buffer buffer,uint64_t n){VkDescriptorBufferInfo info{buffer.handle,0,n};VkWriteDescriptorSet w{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};w.dstSet=sets[set];w.dstBinding=slot;w.descriptorCount=1;w.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;w.pBufferInfo=&info;r.vkUpdateDescriptorSets(r.device,1,&w,0,nullptr);};
 auto writeImage=[&](int set,int slot,VkImageView image,VkSampler sampler){VkDescriptorImageInfo info{sampler,image,image?VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL:VK_IMAGE_LAYOUT_UNDEFINED};VkWriteDescriptorSet w{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};w.dstSet=sets[set];w.dstBinding=slot;w.descriptorCount=1;w.descriptorType=image?VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE:VK_DESCRIPTOR_TYPE_SAMPLER;w.pImageInfo=&info;r.vkUpdateDescriptorSets(r.device,1,&w,0,nullptr);};
 writeBuffer(0,0,matrixBuffer,64);writeBuffer(1,0,profileBuffer,32);writeBuffer(1,1,heightBuffer,400);writeBuffer(1,2,fogBuffer,400);
 writeImage(0,1,parts[6].view,{});writeImage(0,2,{},clamp);
 for(int i=0;i<5;++i)writeImage(1,3+i,parts[i<3?i:i+1].view,{});
 writeImage(1,8,{},wrap);writeImage(1,9,{},clamp);
 std::vector<VkDescriptorImageInfo>bindless(32768,{VK_NULL_HANDLE,parts[7].view,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL});
 for(int i=0;i<100;++i){bindless[1+i].imageView=parts[7+2*i].view;bindless[101+i].imageView=parts[8+2*i].view;}
 VkWriteDescriptorSet bw{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};bw.dstSet=sets[4];bw.dstBinding=0;bw.descriptorCount=32768;bw.descriptorType=VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;bw.pImageInfo=bindless.data();r.vkUpdateDescriptorSets(r.device,1,&bw,0,nullptr);
 VkPipelineLayoutCreateInfo pl{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};pl.setLayoutCount=5;pl.pSetLayouts=layouts.data();OK(r.vkCreatePipelineLayout(r.device,&pl,nullptr,&r.pipelineLayout));
 VkAttachmentDescription attachment{};attachment.format=VK_FORMAT_R8G8B8A8_UNORM;attachment.samples=VK_SAMPLE_COUNT_1_BIT;attachment.loadOp=VK_ATTACHMENT_LOAD_OP_CLEAR;attachment.storeOp=VK_ATTACHMENT_STORE_OP_STORE;attachment.stencilLoadOp=VK_ATTACHMENT_LOAD_OP_DONT_CARE;attachment.stencilStoreOp=VK_ATTACHMENT_STORE_OP_DONT_CARE;attachment.initialLayout=VK_IMAGE_LAYOUT_UNDEFINED;attachment.finalLayout=VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
 VkAttachmentReference color{0,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};VkSubpassDescription sub{};sub.pipelineBindPoint=VK_PIPELINE_BIND_POINT_GRAPHICS;sub.colorAttachmentCount=1;sub.pColorAttachments=&color;
 std::array<VkSubpassDependency,2>deps{{{VK_SUBPASS_EXTERNAL,0,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,0,VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,0},{0,VK_SUBPASS_EXTERNAL,VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT,0}}};
 VkRenderPassCreateInfo rp{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};rp.attachmentCount=1;rp.pAttachments=&attachment;rp.subpassCount=1;rp.pSubpasses=&sub;rp.dependencyCount=2;rp.pDependencies=deps.data();OK(r.vkCreateRenderPass(r.device,&rp,nullptr,&r.renderPass));
 auto image=r.image(resolution,resolution,VK_FORMAT_R8G8B8A8_UNORM,false,VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
 VkFramebufferCreateInfo fb{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};fb.renderPass=r.renderPass;fb.attachmentCount=1;fb.pAttachments=&image.view;fb.width=fb.height=resolution;fb.layers=1;OK(r.vkCreateFramebuffer(r.device,&fb,nullptr,&r.framebuffer));
 auto vs=r.shader(root/L"shader/vsShaderCode.spv"),ps=r.shader(root/L"shader/fsShaderCode.spv");
 std::array<VkPipelineShaderStageCreateInfo,2>stages{};for(auto&s:stages)s.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
 stages[0].pName="vs_main";stages[1].pName="fs_main";
 stages[0].stage=VK_SHADER_STAGE_VERTEX_BIT;stages[0].module=vs;stages[1].stage=VK_SHADER_STAGE_FRAGMENT_BIT;stages[1].module=ps;
 struct Vertex{float x,y,r,g,b,a,u,v;};float u=float(rect[0])/10240.f,v=float(rect[1])/10240.f,du=float(rect[2])/10240.f,dv=float(rect[3])/10240.f;
 std::array<Vertex,3>vertices{{{-1,1,1,0,0,1,u,v},{3,1,1,0,0,1,u+2*du,v},{-1,-3,1,0,0,1,u,v+2*dv}}};
 auto vertexBuffer=r.buffer(sizeof(vertices),VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,vertices.data());
 VkVertexInputBindingDescription vb{0,sizeof(Vertex),VK_VERTEX_INPUT_RATE_VERTEX};std::array<VkVertexInputAttributeDescription,3>attrs{{{0,0,VK_FORMAT_R32G32_SFLOAT,0},{4,0,VK_FORMAT_R32G32B32A32_SFLOAT,8},{6,0,VK_FORMAT_R32G32_SFLOAT,24}}};
 VkPipelineVertexInputStateCreateInfo vi{VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};vi.vertexBindingDescriptionCount=1;vi.pVertexBindingDescriptions=&vb;vi.vertexAttributeDescriptionCount=3;vi.pVertexAttributeDescriptions=attrs.data();
 VkPipelineInputAssemblyStateCreateInfo ia{VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO};ia.topology=VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
 VkViewport viewport{0,0,float(resolution),float(resolution),0,1};VkRect2D scissor{{0,0},{uint32_t(resolution),uint32_t(resolution)}};
 VkPipelineViewportStateCreateInfo vp{VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO};vp.viewportCount=1;vp.pViewports=&viewport;vp.scissorCount=1;vp.pScissors=&scissor;
 VkPipelineRasterizationStateCreateInfo raster{VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO};raster.polygonMode=VK_POLYGON_MODE_FILL;raster.cullMode=VK_CULL_MODE_NONE;raster.frontFace=VK_FRONT_FACE_COUNTER_CLOCKWISE;raster.lineWidth=1;
 VkPipelineMultisampleStateCreateInfo multi{VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO};multi.rasterizationSamples=VK_SAMPLE_COUNT_1_BIT;
 VkPipelineColorBlendAttachmentState blendAttachment{};blendAttachment.colorWriteMask=15;
 VkPipelineColorBlendStateCreateInfo blend{VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO};blend.attachmentCount=1;blend.pAttachments=&blendAttachment;
 VkGraphicsPipelineCreateInfo graphics{VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};graphics.stageCount=2;graphics.pStages=stages.data();graphics.pVertexInputState=&vi;graphics.pInputAssemblyState=&ia;graphics.pViewportState=&vp;graphics.pRasterizationState=&raster;graphics.pMultisampleState=&multi;graphics.pColorBlendState=&blend;graphics.layout=r.pipelineLayout;graphics.renderPass=r.renderPass;
 OK(r.vkCreateGraphicsPipelines(r.device,{},1,&graphics,nullptr,&r.pipeline));
 VkClearValue clear{};VkRenderPassBeginInfo begin{VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};begin.renderPass=r.renderPass;begin.framebuffer=r.framebuffer;begin.renderArea=scissor;begin.clearValueCount=1;begin.pClearValues=&clear;
 r.vkCmdBeginRenderPass(r.command,&begin,VK_SUBPASS_CONTENTS_INLINE);r.vkCmdBindPipeline(r.command,VK_PIPELINE_BIND_POINT_GRAPHICS,r.pipeline);
 VkDeviceSize offset=0;r.vkCmdBindVertexBuffers(r.command,0,1,&vertexBuffer.handle,&offset);r.vkCmdBindDescriptorSets(r.command,VK_PIPELINE_BIND_POINT_GRAPHICS,r.pipelineLayout,0,5,sets.data(),0,nullptr);r.vkCmdDraw(r.command,3,1,0,0);r.vkCmdEndRenderPass(r.command);
 auto readback=r.buffer(uint64_t(resolution)*resolution*4,VK_BUFFER_USAGE_TRANSFER_DST_BIT);
 VkBufferImageCopy copy{};copy.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};copy.imageExtent={uint32_t(resolution),uint32_t(resolution),1};r.vkCmdCopyImageToBuffer(r.command,image.handle,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,readback.handle,1,&copy);
 VkBufferMemoryBarrier barrier{VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER};barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_HOST_READ_BIT;barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.buffer=readback.handle;barrier.offset=0;barrier.size=VK_WHOLE_SIZE;
 r.vkCmdPipelineBarrier(r.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,0,nullptr,1,&barrier,0,nullptr);
 OK(r.vkEndCommandBuffer(r.command));VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.commandBufferCount=1;submit.pCommandBuffers=&r.command;OK(r.vkQueueSubmit(r.queue,1,&submit,{}));OK(r.vkQueueWaitIdle(r.queue));
 void*mapped=nullptr;OK(r.vkMapMemory(r.device,readback.memory,0,VK_WHOLE_SIZE,0,&mapped));
 auto*rgba=static_cast<uint8_t*>(mapped);std::ofstream out(output,std::ios::binary);std::vector<char>row(size_t(resolution)*3);
 for(int y=0;y<resolution;++y){for(int x=0;x<resolution;++x)memcpy(row.data()+x*3,rgba+(size_t(y)*resolution+x)*4,3);out.write(row.data(),row.size());}
 if(!out)throw std::runtime_error("RGB output write failed");out.close();r.vkUnmapMemory(r.device,readback.memory);
 std::cout<<"Original SPIR-V; original vertex shader; 32768-entry shared image binding; "<<resolution<<" square crop"<<std::endl;
 return 0;
}catch(const std::exception&e){std::cerr<<e.what()<<std::endl;return 1;}}
