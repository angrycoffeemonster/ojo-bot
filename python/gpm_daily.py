#!/usr/bin/env python
#
# Processes GPM Daily Products using Late Data
#	It will acquire the 1-d, 3-d and 7-d products for IMERG Late GIS
#	It will generate regional products as requested

import os, inspect, sys, math, urllib, glob, shutil
import argparse

import datetime
from datetime import date, timedelta

from dateutil.parser import parse
from osgeo import gdal
import numpy
import json
from ftplib import FTP

import config

from browseimage import MakeBrowseImage, wms
from s3 import CopyToS3

verbose 		= 0
force 			= 0
ftp_site 		= "jsimpson.pps.eosdis.nasa.gov"

late_gis_path	= "/data/imerg/gis/"
	
def execute( cmd ):
	if verbose:
		print cmd
	os.system(cmd)

def get_late_gpm_files(gis_files, product_name):
	global force, verbose
	downloaded_files = []
		
	try:
		ftp = FTP(ftp_site)
	
		ftp.login('pat@cappelaere.com','pat@cappelaere.com')               					# user anonymous, passwd anonymous@
	
	except Exception as e:
		print "FTP login Error", sys.exc_info()[0], e
		print "Exception", e
		sys.exit(-1)

	for f in gis_files:
		# Check the year and month, we may be ahead
		arr 	= f.split(".")
		ymdarr	= arr[4].split("-")
		ymd		= ymdarr[0]
		month	= ymd[4:6]
		mydir	= os.path.join(config.data_dir, product_name, ymd)
		
		if not os.path.exists(mydir):
		    os.makedirs(mydir)
			
		filepath = late_gis_path+ "%s" % ( month)
		
		ftp.cwd(filepath)
		local_filename = os.path.join(mydir, f)
		if not os.path.exists(local_filename):
			file = open(local_filename, 'wb')
			try:
				ftp.retrbinary("RETR " + f, file.write)
				if verbose:
					print "Downloading...", f, " to ", local_filename
				file.close()
				downloaded_files.append(f)
			except Exception as e:
				if verbose:
					print "GPM IMERG FTP Error", filepath, e					
				os.remove(local_filename)
		else:
			if verbose:
				print "no downloading", local_filename

	ftp.close()
	return gis_files
	
def ValidateRegions(regions):
	for r in regions:
		if not config.regions[r]:
			print "Invalid region", r
			sys.exit(-1)

def CreateLevel(l, geojsonDir, fileName, src_ds, data, attr, regionName):
	global force, verbose
		
	minl				= l
	projection  		= src_ds.GetProjection()
	geotransform		= src_ds.GetGeoTransform()
	#band				= src_ds.GetRasterBand(1)
		
	xorg				= geotransform[0]
	yorg  				= geotransform[3]
	pres				= geotransform[1]
	xmax				= xorg + geotransform[1]* src_ds.RasterXSize
	ymax				= yorg - geotransform[1]* src_ds.RasterYSize

	if not force and os.path.exists(fileName):
		return
		
	driver 				= gdal.GetDriverByName( "GTiff" )

	dst_ds_dataset		= driver.Create( fileName, src_ds.RasterXSize, src_ds.RasterYSize, 1, gdal.GDT_Byte, [ 'COMPRESS=DEFLATE' ] )
	dst_ds_dataset.SetGeoTransform( geotransform )
	dst_ds_dataset.SetProjection( projection )
	o_band		 		= dst_ds_dataset.GetRasterBand(1)
	o_data				= o_band.ReadAsArray(0, 0, dst_ds_dataset.RasterXSize, dst_ds_dataset.RasterYSize )
	
	o_data[data>=l]		= 255
	o_data[data<l]		= 0

	count 				= (o_data > 0).sum()	
	if verbose:
		print "Level", minl, " count:", count

	if count > 0 :

		dst_ds_dataset.SetGeoTransform( geotransform )
			
		dst_ds_dataset.SetProjection( projection )
		
		o_band.WriteArray(o_data, 0, 0)
		
		ct = gdal.ColorTable()
		ct.SetColorEntry( 0, (255, 255, 255, 255) )
		ct.SetColorEntry( 255, (255, 0, 0, 255) )
		o_band.SetRasterColorTable(ct)
		
		dst_ds_dataset 	= None
		if verbose:
			print "Created", fileName

		cmd = "gdal_translate -q -of PNM " + fileName + " "+fileName+".pgm"
		execute(cmd)

		# -i  		invert before processing
		# -t 2  	suppress speckles of up to this many pixels. 
		# -a 1.5  	set the corner threshold parameter
		# -z black  specify how to resolve ambiguities in path decomposition. Must be one of black, white, right, left, minority, majority, or random. Default is minority
		# -x 		scaling factor
		# -L		left margin
		# -B		bottom margin

		cmd = str.format("potrace -i -z black -a 1.5 -t 3 -b geojson -o {0} {1} -x {2} -L {3} -B {4} ", fileName+".geojson", fileName+".pgm", pres, xorg, ymax ); 
		execute(cmd)

		#cmd = str.format("node set_geojson_property.js --file {0} --prop frost={1}", fileName+".geojson", frost)
		#execute(cmd)
	
		#cmd = str.format("topojson -o {0} --simplify-proportion 0.5 -p {3}={1} -- {3}={2}", fileName+".topojson", l, fileName+".geojson", attr ); 
		quiet = " > /dev/null 2>&1"
		if verbose:
			quiet = " "
			
		if regionName == 'global':
			sp = 0.4	#proportion of points to retain for Visvalingam simplification
		else:
			sp = 0.5
			
		cmd = str.format("topojson --bbox --simplify-proportion {0} -o {1} --no-stitch-poles -p {4}={2} -- {4}={3} {5}", sp, fileName+".topojson", minl, fileName+".geojson", attr, quiet ); 
		execute(cmd)
	
		# convert it back to json
		cmd = "topojson-geojson --precision 4 -o %s %s" % ( geojsonDir, fileName+".topojson" )
		execute(cmd)
	
		# rename file
		output_file = "%s_level_%d.geojson" % (attr, minl)
		json_file	= "%s.json" % attr
		cmd 		= "mv %s %s" % (os.path.join(geojsonDir,json_file), os.path.join(geojsonDir, output_file))
		execute(cmd)

#
# Return appropriate color table for product (and levels)
#
def color_table(name):
	color_file = None
	
	if name=='gpm_1d':
		color_file	= os.path.join(basedir, "cluts", "gpm_1d.txt")
	if name=='gpm_3d':
		color_file	= os.path.join(basedir, "cluts", "gpm_3d.txt")
	if name=='gpm_7d':
		color_file	= os.path.join(basedir, "cluts", "gpm_7d.txt")
	if name=='gpm_30mn':
		color_file	= os.path.join(basedir, "cluts", "gpm_30mn.txt")
	if name=='gpm_3hrs':
		color_file	= os.path.join(basedir, "cluts", "gpm_3hrs.txt")
	
	if (color_file == None) or not os.path.exists(color_file):
		print "Invalid color table for", name
		sys.exit(-1)
			
	return color_file
	
#
# Process for a particular region
#
def process(gpm_dir, name, gis_file, ymd, regionName, s3_bucket, s3_folder, levels, hexColors):
	global force, verbose
	region					= config.regions[regionName]

	region_dir	= os.path.join(gpm_dir,regionName)
	if not os.path.exists(region_dir):            
		os.makedirs(region_dir)

	origFileName 			= os.path.join(gpm_dir,gis_file)
	
	print "processing ", regionName, name, gis_file
	
	if not os.path.exists(origFileName):
		print "File does not exist", origFileName
		return

	#
	# subset the file for that region
	#
	bbox					= region['bbox']
	
	subset_file				= os.path.join(region_dir, "%s.%s_ss.tif" % (name,ymd))
	geojsonDir				= os.path.join(region_dir,"geojson_%s" % (name))
	levelsDir				= os.path.join(region_dir,"levels_%s" % (name))

	origFileName_tfw		= origFileName.replace(".tif", ".tfw")
	
	supersampled_file		= os.path.join(region_dir, "%s.%s_x2.tif" % (name, ymd))
	merge_filename 			= os.path.join(geojsonDir, "..", "%s.%s.geojson" % (name, ymd))
	topojson_filename 		= os.path.join(geojsonDir, "..", "%s.%s.topojson" % (name,ymd))
	topojson_gz_filename 	= os.path.join(region_dir, "%s.%s.topojson.gz" % (name,ymd))
	browse_filename 		= os.path.join(geojsonDir, "..", "%s.%s_browse.tif" % (name,ymd))
	subset_aux_filename 	= os.path.join(geojsonDir, "..", "%s.%s_small_browse.tif.aux.xml" % (name, ymd))
	subset_filename 		= os.path.join(geojsonDir, "..", "%s.%s_small_browse.tif" % (name, ymd))
	
	osm_bg_image			= os.path.join(gpm_dir, "..", "%s_osm_bg.png" % regionName)	
	
	sw_osm_image			= os.path.join(region_dir, "%s.%s_thn.png" % (name, ymd))
	tif_image				= os.path.join(region_dir, "%s.%s.tif" % (name, ymd))
	rgb_tif_image			= os.path.join(region_dir, "%s.%s.rgb.tif" % (name, ymd))
	#geojson_filename 		= os.path.join(region_dir, "..", "%s.%s.json" % (name,ymd))
	
	# subset
	if force or not os.path.exists(subset_file):
		cmd = "gdalwarp -overwrite -q -te %f %f %f %f %s %s" % (bbox[0], bbox[1], bbox[2], bbox[3], origFileName, subset_file)
		execute(cmd)
	
	ds 						= gdal.Open(origFileName)
	geotransform			= ds.GetGeoTransform()

	xorg					= geotransform[0]
	yorg  					= geotransform[3]
	pixelsize				= geotransform[1]
	
	if regionName == 'global':
		pixelsize /= 2
		method		= 'near'
	else:
		pixelsize /= 5
		method		= 'near'
	
	# supersample
	if force or not os.path.exists(supersampled_file):
		cmd 			= "gdalwarp -overwrite -q -r %s -tr %f %f -te %f %f %f %f -co COMPRESS=LZW %s %s"%(method, pixelsize, pixelsize, bbox[0], bbox[1], bbox[2], bbox[3], subset_file, supersampled_file)
		execute(cmd)
	
	if verbose:
		color_file		= color_table(name)
		if force or (verbose and not os.path.exists(rgb_tif_image)):	
			cmd = "gdaldem color-relief -q -alpha -of GTiff %s %s %s" % ( supersampled_file, color_file, rgb_tif_image)
			execute(cmd)
		
	if not os.path.exists(geojsonDir):            
		os.makedirs(geojsonDir)

	if not os.path.exists(levelsDir):            
		os.makedirs(levelsDir)
	
	ds 					= gdal.Open(supersampled_file)
	band				= ds.GetRasterBand(1)
	data				= band.ReadAsArray(0, 0, ds.RasterXSize, ds.RasterYSize )
	geotransform		= ds.GetGeoTransform()

	xorg				= geotransform[0]
	yorg  				= geotransform[3]
	pixelsize			= geotransform[1]
	xmax				= xorg + geotransform[1]* ds.RasterXSize
	ymax				= yorg - geotransform[1]* ds.RasterYSize

	data[data>9000]		= 0					# No value
	sdata 				= data/10			# back to mm
	
	if regionName != 'global':
		# Invoke the node script to subset the global geojson	
		global_dir			= os.path.join(gpm_dir,"global")
		global_geojson 		= os.path.join(global_dir, "%s.%s.geojson" % (name,ymd))

		if not os.path.exists(global_geojson):
			print "missing global geojson", global_geojson
			sys.exit(-1)
			
		print "doing regionsl subset...", regionName, global_geojson
		cmd = "node ../subsetregions.js "+regionName+ " " + global_geojson
		execute(cmd)	
		
	else:		
		if force or not os.path.exists(topojson_filename+".gz"):
			for idx, l in enumerate(levels):
				#print "level", idx
				#if idx < len(levels)-1:
				fileName 		= os.path.join(levelsDir, ymd+"_level_%d.tif"%l)
				#CreateLevel(l, levels[idx+1], geojsonDir, fileName, ds, sdata, "precip")
				CreateLevel(l, geojsonDir, fileName, ds, sdata, "precip", regionName)
	
			jsonDict = dict(type='FeatureCollection', features=[])
	
			for idx, l in enumerate(levels):
				fileName 		= os.path.join(geojsonDir, "precip_level_%d.geojson"%l)
				if os.path.exists(fileName):
					with open(fileName) as data_file:    
						jdata = json.load(data_file)
		
					if 'features' in jdata:
						for f in jdata['features']:
							jsonDict['features'].append(f)
	

			with open(merge_filename, 'w') as outfile:
			    json.dump(jsonDict, outfile)	

			quiet = " > /dev/null 2>&1"
			if verbose:
				quiet = " "
				
			# Convert to topojson
			cmd 	= "topojson --no-stitch-poles --bbox -p precip -o "+ topojson_filename + " " + merge_filename + quiet
			execute(cmd)

			if verbose:
				keep = " --keep "
			else:
				keep = " "

			cmd 	= "gzip -f "+keep+ topojson_filename
			execute(cmd)	
	
	if not os.path.exists(osm_bg_image):
		#if verbose:
		print "calling wms", regionName, ymax, xorg, yorg, xmax, osm_bg_image
		wms(yorg, xorg, ymax, xmax, osm_bg_image)

	def scale(x): return x*10
	adjusted_levels = map(scale, levels)

	zoom = 2
	if force or not os.path.exists(sw_osm_image):
		MakeBrowseImage(ds, browse_filename, subset_filename, osm_bg_image, sw_osm_image, list(reversed(adjusted_levels)), list(reversed(hexColors)), force, verbose, zoom)

	if force or not os.path.exists(tif_image):
		cmd 				= "gdalwarp -overwrite -q -co COMPRESS=LZW %s %s"%( subset_file, tif_image)
		execute(cmd)
		
	ds = None
	
	file_list = [ sw_osm_image, topojson_filename+".gz", tif_image ]
	CopyToS3( s3_bucket, s3_folder, file_list, force, verbose )
	
	if not verbose: # Cleanup
		if config.USING_AWS_S3_FOR_STORAGE:		# moved to end
			cmd = "rm -rf %s " % (gpm_dir)
			#print cmd
			#execute(cmd)
		else:
			cmd = "rm -rf %s %s %s %s %s %s %s %s %s %s %s %s" % (origFileName, origFileName_tfw, supersampled_file, merge_filename, topojson_filename, subset_aux_filename, browse_filename, subset_filename, subset_file, rgb_tif_image, geojsonDir, levelsDir)
			execute(cmd)

#	
# ===============================
# Main
#
# python gpm_daily.py --date 2016-05-01 --regions 'global,d02' -v -f

if __name__ == '__main__':
	
	aws_access_key 			= os.environ.get('AWS_ACCESSKEYID')
	aws_secret_access_key 	= os.environ.get('AWS_SECRETACCESSKEY')
	assert(aws_access_key)
	assert(aws_secret_access_key)
	
	parser = argparse.ArgumentParser(description='Generate GPM Rainfall Accumulation Products')
	apg_input = parser.add_argument_group('Input')
	apg_input.add_argument("-f", "--force", action='store_true', help="forces products to be generated")
	apg_input.add_argument("-v", "--verbose", action='store_true', help="Verbose on/off")
	apg_input.add_argument("-d", "--date", help="--date 2015-03-20 or today if not defined")
	apg_input.add_argument("-r", "--regions", help="--regions 'global,d02,d03' ")
	
	options 			= parser.parse_args()
	force				= options.force
	verbose				= options.verbose
	regions				= options.regions.split(',')

	ValidateRegions(regions)
	
	dt					= options.date 
	
	if not dt:
		utc				= datetime.datetime.utcnow()
		#print "GPM daily current utc: ", utc
		hour			= utc.hour
		
		today			= datetime.datetime( utc.year, utc.month, utc.day, hour, 0) + datetime.timedelta(days= -1)
		dt				= today.strftime("%Y-%m-%dT%H:%M:00")

	print "GPM daily for previous day: ", dt
	
	basedir 			= os.path.dirname(os.path.realpath(sys.argv[0]))
	
	today				= parse(dt)
	year				= today.year
	month				= today.month
	day					= today.day
	doy					= today.strftime('%j')
	ymd 				= "%d%02d%02d" % (year, month, day)		
	
	gis_file_day		= "3B-HHR-L.MS.MRG.3IMERG.%d%02d%02d-S233000-E235959.1410.V03E.1day.tif"%(year, month, day)
	gis_file_day_tfw 	= "3B-HHR-L.MS.MRG.3IMERG.%d%02d%02d-S233000-E235959.1410.V03E.1day.tfw"%(year, month, day)

	gis_file_3day		= "3B-HHR-L.MS.MRG.3IMERG.%d%02d%02d-S233000-E235959.1410.V03E.3day.tif"%(year, month, day)
	gis_file_3day_tfw 	= "3B-HHR-L.MS.MRG.3IMERG.%d%02d%02d-S233000-E235959.1410.V03E.3day.tfw"%(year, month, day)

	gis_file_7day		= "3B-HHR-L.MS.MRG.3IMERG.%d%02d%02d-S233000-E235959.1410.V03E.7day.tif"%(year, month, day)
	gis_file_7day_tfw 	= "3B-HHR-L.MS.MRG.3IMERG.%d%02d%02d-S233000-E235959.1410.V03E.7day.tfw"%(year, month, day)
	
	
	#
	# 12 colors - do not change for products (only levels may change)
	#   from low intensity to high intensity Green to Red
	#
	hexColors     		= [ "#c0c0c0", "#018414","#018c4e","#02b331","#57d005","#b5e700","#f9f602","#fbc500","#FF9400","#FE0000","#C80000","#8F0000"]	
	#products 			= ['gpm_1d', 'gpm_3d', 'gpm_7d']
	products 			= ['gpm_1d']
    
	levels 				= [ 1,2,3,5,10,20,40,70,120,200,350,600]

	for p in products:
		for r in regions:
			region				= config.regions[r]
			s3_bucket			= region['bucket']
		
			product_name		= p
	
			s3_folder			= os.path.join(product_name, str(year), doy)
	
			gpm_dir				= os.path.join(config.data_dir, product_name, ymd)
			if not os.path.exists(gpm_dir):
			    os.makedirs(gpm_dir)
	
			get_late_gpm_files([gis_file_day, gis_file_day_tfw], product_name)
			process(gpm_dir, product_name, gis_file_day, ymd, r, s3_bucket, s3_folder, levels, hexColors)

	#
	# Cleanup
	#
	if not verbose:
		for p in products:
			gpm_dir		= os.path.join(config.data_dir, p, ymd)
			for r in regions:
				region_dir = os.path.join(gpm_dir, r)
				if config.USING_AWS_S3_FOR_STORAGE: # Full Cleanup
					cmd = "rm -rf %s " % ( region_dir)
		