// Deterministic WIC JPEG Q100 4:4:4 conversion and full decoded-pixel comparison.
#define NOMINMAX
#include <windows.h>
#include <wincodec.h>
#include <wrl/client.h>
#include <bcrypt.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <vector>
#pragma comment(lib,"ole32.lib")
#pragma comment(lib,"oleaut32.lib")
#pragma comment(lib,"windowscodecs.lib")
#pragma comment(lib,"bcrypt.lib")
using Microsoft::WRL::ComPtr;
static void check(HRESULT h){if(FAILED(h)){std::ostringstream s;s<<"HRESULT 0x"<<std::hex<<uint32_t(h);throw std::runtime_error(s.str());}}
static std::string hash(const std::vector<BYTE>& bytes){
    BCRYPT_ALG_HANDLE a=nullptr;BCRYPT_HASH_HANDLE h=nullptr;DWORD n=0,len=0;
    if(BCryptOpenAlgorithmProvider(&a,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0)throw std::runtime_error("SHA256 open failed");
    BCryptGetProperty(a,BCRYPT_OBJECT_LENGTH,reinterpret_cast<BYTE*>(&len),sizeof(len),&n,0);
    std::vector<BYTE> object(len);std::array<BYTE,32> out;
    if(BCryptCreateHash(a,&h,object.data(),len,nullptr,0,0)<0||
        BCryptHashData(h,const_cast<BYTE*>(bytes.data()),ULONG(bytes.size()),0)<0||BCryptFinishHash(h,out.data(),32,0)<0)
        throw std::runtime_error("SHA256 failed");
    BCryptDestroyHash(h);BCryptCloseAlgorithmProvider(a,0);std::ostringstream s;
    for(BYTE b:out)s<<std::hex<<std::setfill('0')<<std::setw(2)<<unsigned(b);return s.str();
}
struct Image{UINT w,h;std::vector<BYTE> rgb;};
static Image read(IWICImagingFactory* f,const wchar_t* path){
    ComPtr<IWICBitmapDecoder> decoder;check(f->CreateDecoderFromFilename(path,nullptr,GENERIC_READ,WICDecodeMetadataCacheOnLoad,&decoder));
    ComPtr<IWICBitmapFrameDecode> frame;check(decoder->GetFrame(0,&frame));Image i;check(frame->GetSize(&i.w,&i.h));
    if(!i.w||!i.h||uint64_t(i.w)*i.h*3>0xffffffff)throw std::runtime_error("Unexpected image dimensions");
    ComPtr<IWICFormatConverter> converter;check(f->CreateFormatConverter(&converter));
    check(converter->Initialize(frame.Get(),GUID_WICPixelFormat24bppRGB,WICBitmapDitherTypeNone,nullptr,0,WICBitmapPaletteTypeCustom));
    i.rgb.resize(size_t(i.w)*i.h*3);check(converter->CopyPixels(nullptr,i.w*3,UINT(i.rgb.size()),i.rgb.data()));return i;
}
static void encode(IWICImagingFactory* f,const Image& i,const wchar_t* path){
    if(GetFileAttributesW(path)!=INVALID_FILE_ATTRIBUTES)throw std::runtime_error("Refusing to overwrite existing output");
    ComPtr<IWICStream> stream;check(f->CreateStream(&stream));check(stream->InitializeFromFilename(path,GENERIC_WRITE));
    ComPtr<IWICBitmapEncoder> encoder;check(f->CreateEncoder(GUID_ContainerFormatJpeg,nullptr,&encoder));
    check(encoder->Initialize(stream.Get(),WICBitmapEncoderNoCache));
    ComPtr<IWICBitmapFrameEncode> frame;ComPtr<IPropertyBag2> options;check(encoder->CreateNewFrame(&frame,&options));
    PROPBAG2 properties[2]{};properties[0].pstrName=const_cast<LPOLESTR>(L"ImageQuality");
    properties[1].pstrName=const_cast<LPOLESTR>(L"JpegYCrCbSubsampling");
    VARIANT values[2];VariantInit(&values[0]);VariantInit(&values[1]);
    values[0].vt=VT_R4;values[0].fltVal=1.0f;values[1].vt=VT_UI1;values[1].bVal=WICJpegYCrCbSubsampling444;
    check(options->Write(2,properties,values));check(frame->Initialize(options.Get()));check(frame->SetSize(i.w,i.h));
    WICPixelFormatGUID format=GUID_WICPixelFormat24bppBGR;check(frame->SetPixelFormat(&format));
    if(format!=GUID_WICPixelFormat24bppBGR)throw std::runtime_error("Unexpected encoder pixel format");
    std::vector<BYTE> bgr=i.rgb;for(size_t p=0;p<bgr.size();p+=3)std::swap(bgr[p],bgr[p+2]);
    check(frame->WritePixels(i.h,i.w*3,UINT(bgr.size()),bgr.data()));check(frame->Commit());check(encoder->Commit());
}
int wmain(int argc,wchar_t** argv){
    try{
        if(argc!=3)throw std::runtime_error("Arguments: accepted.png fresh.jpg");
        check(CoInitializeEx(nullptr,COINIT_MULTITHREADED));ComPtr<IWICImagingFactory> factory;
        check(CoCreateInstance(CLSID_WICImagingFactory,nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&factory)));
        Image source=read(factory.Get(),argv[1]);encode(factory.Get(),source,argv[2]);Image jpeg=read(factory.Get(),argv[2]);
        if(source.w!=jpeg.w||source.h!=jpeg.h)throw std::runtime_error("Image dimensions changed");
        std::array<uint64_t,256> histogram{};uint64_t sum_abs=0,sum_sq=0,edge_abs=0,edge_count=0;
        int maximum=0,edge_max=0;size_t worst=0;double luma_sq=0;
        for(size_t p=0;p<source.rgb.size();p+=3){
            int largest=0;double luma=0;bool edge=false;
            for(size_t c=0;c<3;c++){
                int d=int(jpeg.rgb[p+c])-int(source.rgb[p+c]),a=std::abs(d);sum_abs+=a;sum_sq+=d*d;
                if(a>maximum){maximum=a;worst=p/3;}largest=std::max(largest,a);luma+=d*(c==0?.299:c==1?.587:.114);
                if((p/3)%source.w && std::abs(int(source.rgb[p+c])-int(source.rgb[p-3+c]))>=8)edge=true;
                if(p>=size_t(source.w)*3 && std::abs(int(source.rgb[p+c])-int(source.rgb[p-source.w*3+c]))>=8)edge=true;
            }
            histogram[largest]++;luma_sq+=luma*luma;
            if(edge){edge_count++;for(size_t c=0;c<3;c++)edge_abs+=std::abs(int(jpeg.rgb[p+c])-int(source.rgb[p+c]));edge_max=std::max(edge_max,largest);}
        }
        double mse=double(sum_sq)/source.rgb.size(),mae=double(sum_abs)/source.rgb.size();
        auto quantile=[&](double q){uint64_t total=0,target=uint64_t(std::ceil(q*source.w*source.h));for(int i=0;i<256;i++){total+=histogram[i];if(total>=target)return i;}return 255;};
        std::cout<<std::setprecision(12)<<"{\"width\":"<<source.w<<",\"height\":"<<source.h
            <<",\"source_rgb_sha256\":\""<<hash(source.rgb)<<"\",\"jpeg_rgb_sha256\":\""<<hash(jpeg.rgb)
            <<"\",\"quality\":100,\"requested_chroma\":\"4:4:4\",\"resampling\":false"
            <<",\"compared_pixels\":"<<uint64_t(source.w)*source.h<<",\"mean_absolute_channel_error\":"<<mae
            <<",\"mean_squared_channel_error\":"<<mse<<",\"psnr_db\":"<<(mse?10*std::log10(65025/mse):999)
            <<",\"luma_psnr_db\":"<<(luma_sq?10*std::log10(65025/(luma_sq/(source.w*source.h))):999)
            <<",\"maximum_channel_error\":"<<maximum<<",\"pixel_error_p99\":"<<quantile(.99)<<",\"pixel_error_p999\":"<<quantile(.999)
            <<",\"worst_pixel\":["<<worst%source.w<<","<<worst/source.w<<"],\"edge_pixels\":"<<edge_count
            <<",\"edge_mean_absolute_channel_error\":"<<(edge_count?double(edge_abs)/(edge_count*3):0)<<",\"edge_maximum_error\":"<<edge_max
            <<",\"pixel_max_error_histogram\":[";
        for(size_t j=0;j<256;j++){if(j)std::cout<<",";std::cout<<histogram[j];}std::cout<<"]}\n";
        return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}
}
