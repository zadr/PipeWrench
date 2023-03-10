Pod::Spec.new do |s|
  s.name               = "PipeWrench"
  s.version            = "0.1.0"
  s.summary            = "An iOS library can detect leaks in tests"
  s.description        = <<-DESC
                          Use PipeWrench in your test targets to detect leaks automatically
                          DESC
  s.homepage           = "https://github.com/zadr/PipeWrench"
  s.license            = { :type => "MIT", :file => "LICENSE" }
  s.author             = "PipeWrench Contributors"
  s.source             = { :git => "https://github.com/zadr/PipeWrench.git", 
                           :tag => s.version.to_s }
  s.requires_arc       = true
  s.platforms          = { :ios => "15.0" }
  s.swift_version      = "5.0"
  s.source_files       = [
    "PipeWrench/**/*.{swift,h,m,c,mm}"
  ]
  s.weak_frameworks    = "XCTest"
  s.cocoapods_version  = '>= 1.4.0'
end
